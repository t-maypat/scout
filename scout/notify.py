"""The notifier: turn derived state into one Discord message.

A channel webhook needs no bot, no OAuth and no application - you create it in Discord's
UI and POST JSON at it. That is the whole setup for notifications.

Buttons are a different matter. Discord ignores interactive components from a webhook
that is not owned by an application, so a webhook made by hand in the UI can never carry
them however it is called. Buttons require the bot to post, which is why there are two
transports here: bot when a token and channel are configured, webhook otherwise.

The rule this module exists to enforce: **polling often and interrupting often are
different decisions.** The poller runs every fifteen minutes so the log is accurate. This
runs once, in the evening, when there is actually time to act on what it says. Anything
that pings a phone during office hours had better be worth walking out of a meeting for,
which is why the hot path is deliberately narrow.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from scout.derive import Opportunity, Transition
from scout.events import NOTIFICATION_SENT, Event

# Discord's documented ceilings. Exceeding any of them rejects the whole message.
MAX_EMBEDS = 10
MAX_TITLE = 256
MAX_DESCRIPTION = 4096
MAX_TOTAL = 6000

COLOURS = {
    "unassigned": 0x3BA55D,  # just came free - the best kind
    "unanswered-report": 0x5865F2,
    "stale-assignment": 0xE67E22,
    "abandoned-pr": 0x9B59B6,
    "ready_for_review": 0x4F8EF7,
}

HEADLINES = {
    "unassigned": "just came free",
    "unanswered-report": "nobody has replied",
    "stale-assignment": "assigned, then abandoned",
    "abandoned-pr": "half-finished, author gone",
    "ready_for_review": "left draft",
}


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass(frozen=True)
class Item:
    """One line of a digest, from either an opportunity or a transition."""

    key: str
    kind: str
    repo: str
    number: int
    title: str
    url: str
    note: str
    idle_days: int = 0

    @classmethod
    def from_opportunity(cls, o: Opportunity) -> Item:
        return cls(o.key, o.kind, o.repo, o.number, o.title, o.url, o.note, o.idle_days)

    @classmethod
    def from_transition(cls, t: Transition, title: str = "", url: str = "") -> Item:
        note = f"unassigned from {t.detail}" if t.detail else t.what
        return cls(t.key, t.what, t.repo, t.number, title or f"#{t.number}", url, note)


@dataclass
class Digest:
    items: list[Item] = field(default_factory=list)
    skipped: int = 0
    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def empty(self) -> bool:
        return not self.items

    @property
    def keys(self) -> list[str]:
        return [item.key for item in self.items]

    def to_messages(self, with_buttons: bool = False) -> list[dict[str, Any]]:
        """One message, or a header plus one per item when buttons are wanted.

        Discord attaches components to a message rather than an embed, so there is no
        way to put a different pair of buttons under each of eight embeds in one
        message. Per-item actions mean per-item messages.
        """
        if not with_buttons:
            return [self.to_discord()]

        header = self.to_discord()["embeds"][0]
        messages: list[dict[str, Any]] = [{"embeds": [header]}]
        for item in self.items[: MAX_EMBEDS - 1]:
            age = f" - idle {item.idle_days}d" if item.idle_days else ""
            messages.append(
                {
                    "embeds": [
                        {
                            "title": _clip(
                                f"{item.repo}#{item.number} - {item.title}", MAX_TITLE
                            ),
                            "url": item.url,
                            "description": _clip(
                                f"*{HEADLINES.get(item.kind, item.kind)}*{age}"
                                + chr(10)
                                + item.note,
                                MAX_DESCRIPTION,
                            ),
                            "color": COLOURS.get(item.kind, 0x99AAB5),
                        }
                    ],
                    "components": [buttons_for(item)],
                }
            )
        return messages

    def to_discord(self) -> dict[str, Any]:
        """One message: a header embed, then one embed per item.

        Items carry their own colour so the channel is scannable without reading - green
        means something came free, amber means something was abandoned.
        """
        header = {
            "title": f"{len(self.items)} worth a look",
            "description": _clip(
                " · ".join(
                    f"**{n}** {HEADLINES.get(k, k)}"
                    for k, n in _counts(self.items).items()
                )
                + (f"\n{self.skipped} already sent, not repeated." if self.skipped else ""),
                MAX_DESCRIPTION,
            ),
            "color": 0x2B2D31,
            "footer": {"text": f"scout · {self.generated_at:%d %b %H:%M} UTC"},
        }

        embeds = [header]
        for item in self.items[: MAX_EMBEDS - 1]:
            age = f" · idle {item.idle_days}d" if item.idle_days else ""
            embeds.append(
                {
                    "title": _clip(f"{item.repo}#{item.number} — {item.title}", MAX_TITLE),
                    "url": item.url,
                    "description": _clip(
                        f"*{HEADLINES.get(item.kind, item.kind)}*{age}\n{item.note}",
                        MAX_DESCRIPTION,
                    ),
                    "color": COLOURS.get(item.kind, 0x99AAB5),
                }
            )

        payload = {"username": "scout", "embeds": embeds}
        # Trim from the tail rather than let Discord reject the whole message.
        while len(json.dumps(payload)) > MAX_TOTAL and len(payload["embeds"]) > 1:
            payload["embeds"].pop()
        return payload


def _counts(items: Iterable[Item]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        counts[item.kind] = counts.get(item.kind, 0) + 1
    return counts


def already_sent(events: Iterable[Event]) -> set[str]:
    """Keys this channel has seen. A digest that repeats itself gets muted in a week."""
    sent: set[str] = set()
    for event in events:
        if event.kind == NOTIFICATION_SENT:
            sent.update(event.payload.get("keys") or [])
    return sent


def build_digest(
    opportunities: Iterable[Opportunity],
    transitions: Iterable[Transition] = (),
    sent: set[str] | None = None,
    max_items: int = 8,
    titles: dict[str, tuple[str, str]] | None = None,
) -> Digest:
    sent = sent or set()
    titles = titles or {}
    items: list[Item] = []

    # Transitions first: something that just came free is more actionable than something
    # that has been sitting for forty days and will still be there tomorrow.
    for transition in transitions:
        title, url = titles.get(f"{transition.repo}#{transition.number}", ("", ""))
        items.append(Item.from_transition(transition, title, url))
    items.extend(Item.from_opportunity(o) for o in opportunities)

    seen: set[str] = set()
    fresh: list[Item] = []
    skipped = 0
    for item in items:
        if item.key in seen:
            continue
        seen.add(item.key)
        if item.key in sent:
            skipped += 1
            continue
        fresh.append(item)

    return Digest(items=fresh[:max_items], skipped=skipped)


def sent_event(digest: Digest, channel: str = "discord") -> Event:
    """Record what went out, so the next digest can leave it alone."""
    return Event.make(
        kind=NOTIFICATION_SENT,
        repo="-",
        subject=channel,
        occurred_at=digest.generated_at,
        payload={"channel": channel, "keys": digest.keys, "count": len(digest.items)},
    )


LINK, PRIMARY, SECONDARY = 5, 1, 2
ACTION_ROW, BUTTON = 1, 2
API = "https://discord.com/api/v10"


def buttons_for(item: Item) -> dict[str, Any]:
    """One row of actions for one item.

    Components attach to a message, not to an embed, so per-item buttons mean per-item
    messages. That is why bot mode sends a header and then one message each rather than
    a single digest - eight embeds in one message can only ever share one row.

    The link button needs nothing listening: Discord opens the url itself. Only Later and
    Not for me reach the interaction receiver.
    """
    return {
        "type": ACTION_ROW,
        "components": [
            {"type": BUTTON, "style": LINK, "label": "Open on GitHub", "url": item.url},
            {
                "type": BUTTON,
                "style": SECONDARY,
                "label": "Later",
                "custom_id": f"snooze:{item.repo}:{item.number}",
            },
            {
                "type": BUTTON,
                "style": SECONDARY,
                "label": "Not for me",
                "custom_id": f"dismiss:{item.repo}:{item.number}",
            },
        ],
    }


def post(
    payload: dict[str, Any],
    webhook_url: str = "",
    bot_token: str = "",
    channel_id: str = "",
    timeout: float = 15.0,
) -> None:
    """Send one message, as the bot when configured and by webhook otherwise."""
    if bot_token and channel_id:
        response = httpx.post(
            f"{API}/channels/{channel_id}/messages",
            json=payload,
            headers={"Authorization": f"Bot {bot_token}"},
            timeout=timeout,
        )
    elif webhook_url:
        response = httpx.post(webhook_url, json=payload, timeout=timeout)
    else:
        raise ValueError(
            "nowhere to send - set SCOUT_DISCORD_WEBHOOK_URL, or a bot token and "
            "channel id for buttons"
        )

    if response.status_code == 429:
        retry = response.json().get("retry_after", "?")
        raise RuntimeError(f"discord rate limited, retry after {retry}s")
    if response.status_code == 403:
        raise RuntimeError(
            "discord refused: the bot needs View Channel and Send Messages in that "
            "channel, and the channel id must be the one it can see"
        )
    response.raise_for_status()


def send(
    digest: Digest,
    webhook_url: str = "",
    bot_token: str = "",
    channel_id: str = "",
) -> int:
    """Deliver a digest. Returns how many messages went out."""
    messages = digest.to_messages(with_buttons=bool(bot_token and channel_id))
    for message in messages:
        post(message, webhook_url, bot_token, channel_id)
    return len(messages)
