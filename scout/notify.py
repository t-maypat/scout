"""The notifier: turn derived state into Discord messages.

A channel webhook needs no bot, no OAuth and no application - you create it in Discord's
UI and POST JSON at it. That is the whole setup for notifications.

Buttons are a different matter. Discord ignores interactive components from a webhook
that is not owned by an application, so a webhook made by hand in the UI can never carry
them however it is called. Buttons require the bot to post, which is why there are two
transports here: bot when a token and channel are configured, webhook otherwise.

The rule this module exists to enforce: **polling often and interrupting often are
different decisions.** The poller runs on its own schedule so the log is accurate. This
runs once, in the evening, when there is actually time to act on what it says. Anything
that pings a phone during office hours had better be worth walking out of a meeting for,
which is why the hot path is deliberately narrow.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
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
    "fresh-and-free": 0x1ABC9C,
}

HEADLINES = {
    "unassigned": "just came free",
    "unanswered-report": "nobody has replied",
    "stale-assignment": "assigned, then abandoned",
    "abandoned-pr": "half-finished, author gone",
    "ready_for_review": "left draft",
    "fresh-and-free": "fresh, nobody on it",
}


# Threads are per repository per day *per stream*. Work that has been sitting for weeks
# and an issue that opened this morning are read at different speeds and deserve separate
# threads: the fresh one is the one to open first, and it should not be buried under six
# abandoned pull requests.
WORK, FRESH = "work", "fresh"
STREAM_FOR = {"fresh-and-free": FRESH}


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
    stream: str = WORK

    @classmethod
    def from_opportunity(cls, o: Opportunity) -> Item:
        return cls(
            o.key, o.kind, o.repo, o.number, o.title, o.url, o.note, o.idle_days,
            STREAM_FOR.get(o.kind, WORK),
        )

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

    def only(self, keys: Iterable[str]) -> Digest:
        """This digest cut down to these keys - what to record when a send stops partway."""
        wanted = set(keys)
        return Digest(
            items=[item for item in self.items if item.key in wanted],
            skipped=self.skipped,
            generated_at=self.generated_at,
        )

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
            messages.append(item_message(item))
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

# Discord publishes a global limit - 50 requests a second - but no per-route numbers, and
# says not to hard-code them: the response headers are the only authority. A digest is
# about ten requests once a day, so all of these can afford to be conservative.
#
# Between consecutive requests. Eight items posted into one thread back to back is what
# tripped the per-channel bucket; a second apiece costs ten seconds a day.
SEND_INTERVAL = 1.0
# The longest single wait scout takes on Discord's word, whether from `retry_after` or an
# empty bucket's reset. Like GitHub's `retry-after` it is a number the other side chooses,
# so it is capped rather than honoured literally.
MAX_RATE_LIMIT_WAIT = 5.0
# How many 429s one request may answer with a wait before scout gives up on it.
RATE_LIMIT_RETRIES = 3
# Over a whole send. The Actions job is killed at ten minutes and the step after the send
# has to commit what went out, so a send must fail long before the runner is killed.
SEND_DEADLINE = 120.0


class SendFailed(RuntimeError):
    """A send that stopped partway, carrying the keys of the items that reached Discord.

    Without them nothing records those items, and the next digest sends them again.
    """

    def __init__(self, message: str, delivered: Iterable[str] = ()) -> None:
        super().__init__(message)
        self.delivered = list(delivered)


def _retry_after(response: httpx.Response) -> float | None:
    """The wait Discord asked for, or None when it did not say in a usable way."""
    try:
        return float(response.json()["retry_after"])
    except (ValueError, KeyError, TypeError):
        pass
    try:
        return float(response.headers["retry-after"])
    except (KeyError, ValueError):
        return None


class Pacing:
    """Spacing, bucket waits and 429 retries for one send, every one of them bounded.

    Three layers, cheapest first. A fixed interval stops a burst from forming. A bucket
    Discord has reported empty (`X-RateLimit-Remaining: 0`) is waited out before that route
    is asked again. A 429 that gets past both is retried after the `retry_after` it names.
    No wait exceeds MAX_RATE_LIMIT_WAIT and none may run past the deadline: a send that
    cannot finish in time fails and says so, rather than sitting on the runner.
    """

    def __init__(
        self,
        interval: float = SEND_INTERVAL,
        deadline: float = SEND_DEADLINE,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.interval = interval
        self.deadline_seconds = deadline
        self._sleep = sleep
        self._clock = clock
        self._deadline = clock() + deadline
        self._last: float | None = None
        # Route -> when its bucket refills. Keyed by method and path, because the channel
        # id in the path is what Discord scopes a message bucket to.
        self._refills: dict[str, float] = {}

    def _wait(self, seconds: float, why: str) -> None:
        seconds = min(seconds, MAX_RATE_LIMIT_WAIT)
        if seconds <= 0:
            return
        if self._clock() + seconds > self._deadline:
            raise RuntimeError(
                f"discord send would pass its {self.deadline_seconds:.0f}s deadline {why}"
            )
        self._sleep(seconds)

    def _note_bucket(self, route: str, response: httpx.Response) -> None:
        if response.headers.get("x-ratelimit-remaining") != "0":
            return
        try:
            reset_after = float(response.headers["x-ratelimit-reset-after"])
        except (KeyError, ValueError):
            return
        self._refills[route] = self._clock() + reset_after

    def request(self, route: str, send: Callable[[], httpx.Response]) -> httpx.Response:
        """Make one request through the pacing. Returns the first response that is not 429."""
        if self._last is not None:
            self._wait(self._last + self.interval - self._clock(), "between requests")
        refill = self._refills.pop(route, None)
        if refill is not None:
            self._wait(refill - self._clock(), "waiting for an empty bucket to refill")

        attempt = 0
        while True:
            response = send()
            self._last = self._clock()
            self._note_bucket(route, response)
            if response.status_code != 429:
                return response
            wait = _retry_after(response)
            # Sleeping the cap against a longer limit only earns another 429, and each
            # one counts toward Discord's invalid-request ban. Fail instead.
            if wait is None or wait > MAX_RATE_LIMIT_WAIT or attempt >= RATE_LIMIT_RETRIES:
                shown = "?" if wait is None else f"{wait:g}"
                raise RuntimeError(f"discord rate limited, retry after {shown}s")
            self._wait(wait, "waiting out a rate limit")
            attempt += 1


def buttons_for(item: Item) -> dict[str, Any]:
    """One row of actions for one item.

    Components attach to a message, not to an embed, so per-item buttons mean per-item
    messages. That is why bot mode posts one message per item, inside a thread, rather
    than a single digest - eight embeds in one message can only ever share one row.

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


def control_row(repo: str) -> dict[str, Any]:
    """The one button on a thread header: check this repository now.

    Scheduled runs are late - measured at one to six hours on this repository, and GitHub
    documents that schedules are delayed under load and may be dropped. That is fine for a
    log and useless when you have twenty minutes free now, so the header carries a way to
    ask immediately. It queues a workflow; nothing is written to GitHub by the tap.
    """
    return {
        "type": ACTION_ROW,
        "components": [
            {
                "type": BUTTON,
                "style": PRIMARY,
                "label": "Check now",
                "custom_id": f"poll:{repo}",
            }
        ],
    }


def post(
    payload: dict[str, Any],
    webhook_url: str = "",
    bot_token: str = "",
    channel_id: str = "",
    timeout: float = 15.0,
    pacing: Pacing | None = None,
) -> None:
    """Send one message, as the bot when configured and by webhook otherwise."""
    pacing = pacing or Pacing()
    if bot_token and channel_id:
        route = f"POST /channels/{channel_id}/messages"
        url = f"{API}/channels/{channel_id}/messages"
        headers = {"Authorization": f"Bot {bot_token}"}
    elif webhook_url:
        # Not the url: it carries the webhook's token.
        route, url, headers = "POST webhook", webhook_url, {}
    else:
        raise ValueError(
            "nowhere to send - set SCOUT_DISCORD_WEBHOOK_URL, or a bot token and "
            "channel id for buttons"
        )

    response = pacing.request(
        route, lambda: httpx.post(url, json=payload, headers=headers, timeout=timeout)
    )
    if response.status_code == 403:
        raise RuntimeError(
            "discord refused: the bot needs View Channel and Send Messages in that "
            "channel, and the channel id must be the one it can see"
        )
    response.raise_for_status()


def item_message(item: Item) -> dict[str, Any]:
    """One item as one message: its embed and its own row of buttons."""
    age = f" - idle {item.idle_days}d" if item.idle_days else ""
    return {
        "embeds": [
            {
                "title": _clip(f"{item.repo}#{item.number} - {item.title}", MAX_TITLE),
                "url": item.url,
                "description": _clip(
                    f"*{HEADLINES.get(item.kind, item.kind)}*{age}" + chr(10) + item.note,
                    MAX_DESCRIPTION,
                ),
                "color": COLOURS.get(item.kind, 0x99AAB5),
            }
        ],
        "components": [buttons_for(item)],
    }


def by_thread(items: Iterable[Item]) -> dict[tuple[str, str], list[Item]]:
    """Items grouped by the thread they belong in: one per repository per stream.

    The digest is already ranked, so the first group here is the one holding the single
    most actionable thing tonight, and each thread keeps its items in rank order.
    """
    groups: dict[tuple[str, str], list[Item]] = {}
    for item in items:
        groups.setdefault((item.repo, item.stream), []).append(item)
    return groups


def thread_label(repo: str, stream: str, day: str) -> str:
    """What the channel line says. The stream is named only when it is not the usual one,
    so an ordinary day still reads `17 Sep - owner/repo`."""
    return f"{day} - {repo}" if stream == WORK else f"{day} - {repo} - {stream}"


# A day, to match one thread per repository per day.
THREAD_ARCHIVE_MINUTES = 1440
THREAD_STATE_DAYS = 7

Request = Callable[[str, str, dict[str, Any] | None], dict[str, Any]]


def bot_request(token: str, timeout: float = 15.0, pacing: Pacing | None = None) -> Request:
    """Authenticated calls to Discord's API as the bot, paced, with the failures named."""
    pacing = pacing or Pacing()

    def call(method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        response = pacing.request(
            f"{method} {path}",
            lambda: httpx.request(
                method,
                f"{API}{path}",
                json=payload,
                headers={"Authorization": f"Bot {token}"},
                timeout=timeout,
            ),
        )
        if response.status_code == 403:
            raise RuntimeError(
                "discord refused: on the scout channel the bot needs View Channel, Send "
                "Messages, Embed Links, Create Public Threads and Send Messages in Threads"
            )
        response.raise_for_status()
        return response.json() if response.content else {}

    return call


def _load_threads(path: Path) -> dict[str, dict[str, dict[str, Any]]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def _save_threads(path: Path, threads: dict[str, Any], today: date) -> None:
    cutoff = (today - timedelta(days=THREAD_STATE_DAYS)).isoformat()
    kept = {day: repos for day, repos in threads.items() if day >= cutoff}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(kept, indent=2, sort_keys=True), encoding="utf-8")


def send_threaded(
    digest: Digest,
    channel_id: str,
    request: Request,
    state_file: Path,
    today: date,
) -> int:
    """One thread per repository per stream per day, one message per item inside it.

    The channel itself gets a single line per repository per day, so it reads as a
    list of days and projects rather than a wall of items. A second send on the same
    day adds to that repository's existing thread and updates the count on its header
    instead of starting another. State is saved as soon as a thread exists, and again
    when a send fails, so a retry finishes in the same thread. That only holds if the
    state is committed, which is why digest.yml records even after a failed send.

    A failure partway raises SendFailed naming the items that did go out.
    """
    label = today.strftime("%d %b")
    threads = _load_threads(state_file)
    todays = threads.setdefault(today.isoformat(), {})
    sent = 0
    delivered: list[str] = []

    try:
        for (repo, stream), items in by_thread(digest.items).items():
            name = thread_label(repo, stream, label)
            key = f"{repo}:{stream}"
            entry = todays.get(key)
            if entry is None:
                header = request(
                    "POST",
                    f"/channels/{channel_id}/messages",
                    {"content": f"{name} ({len(items)})", "components": [control_row(repo)]},
                )
                thread = request(
                    "POST",
                    f"/channels/{channel_id}/messages/{header['id']}/threads",
                    {
                        "name": _clip(name, 100),
                        "auto_archive_duration": THREAD_ARCHIVE_MINUTES,
                    },
                )
                entry = {"thread": thread["id"], "header": header["id"], "count": 0}
                todays[key] = entry
                sent += 1
                _save_threads(state_file, threads, today)

            for item in items:
                request("POST", f"/channels/{entry['thread']}/messages", item_message(item))
                delivered.append(item.key)
                # Per item, so a send that stops partway leaves a count the retry can fix.
                entry["count"] = int(entry.get("count", 0)) + 1
                sent += 1

            if entry["count"] != len(items):
                request(
                    "PATCH",
                    f"/channels/{channel_id}/messages/{entry['header']}",
                    {"content": f"{name} ({entry['count']})"},
                )
            _save_threads(state_file, threads, today)
    except (RuntimeError, httpx.HTTPError) as exc:
        _save_threads(state_file, threads, today)
        raise SendFailed(str(exc), delivered) from exc

    return sent


def send(
    digest: Digest,
    webhook_url: str = "",
    bot_token: str = "",
    channel_id: str = "",
    state_dir: str = "./data/state",
    tz: str = "UTC",
) -> int:
    """Deliver a digest. Returns how many messages went out.

    As the bot, a thread per repository per day. By webhook, one message: a plain webhook
    can neither start a thread in a text channel nor carry buttons.
    """
    if bot_token and channel_id:
        # The day is the reader's day, not UTC's - otherwise an evening digest in IST
        # lands in yesterday's thread for the first five and a half hours of the date.
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            today = datetime.now(ZoneInfo(tz)).date()
        except ZoneInfoNotFoundError:
            today = datetime.now(UTC).date()
        return send_threaded(
            digest,
            channel_id,
            bot_request(bot_token),
            Path(state_dir) / "discord_threads.json",
            today,
        )
    post(digest.to_discord(), webhook_url)
    return 1
