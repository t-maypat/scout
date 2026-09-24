"""The poller: fetch what changed on the watchlist, turn it into observations.

One REST call per repository per poll, usually. `/issues` returns pull requests too, so
one stable URL covers both - which also means one ETag covers both, and an idle
repository costs zero rate limit.

Budget: ten repositories four times a day is forty requests, against a limit of five
thousand an hour, and most come back 304. The rate limit is not the constraint here and
never will be. What matters is that polling often and *notifying* often are
different decisions: this module only observes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from scout.cursors import Cursor, Cursors, newest
from scout.events import ISSUE_OBSERVED, PR_OBSERVED, Event, parse_time
from scout.github import GitHubClient, GitHubError, NotFound
from scout.probe import split_name

# Stable URL on purpose. A moving `since=` would invalidate the ETag on every poll and
# throw away the only free thing GitHub gives us.
LISTING = "/repos/{owner}/{name}/issues?state=all&sort=updated&direction=desc&per_page={n}"
# The other end of the same list. Sorting newest-first and walking toward a watermark can
# never reach the oldest rows - and the oldest rows are the whole point, because an
# abandoned pull request is one nothing has touched for ninety days. On a repository with
# five thousand open items those sit thousands of rows beyond any page budget worth
# spending. One request at the far end costs a rate-limit point the first time and 304s
# after that, because by definition nothing there is moving.
STALEST = "/repos/{owner}/{name}/issues?state=open&sort=updated&direction=asc&per_page={n}"
MAX_PAGES = 5


@dataclass
class PollResult:
    repo: str
    events: list[Event]
    unchanged: bool = False
    pages: int = 0
    stale_seen: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def _labels(item: dict[str, Any]) -> list[str]:
    return [
        label["name"]
        for label in item.get("labels") or []
        if isinstance(label, dict) and label.get("name")
    ]


def to_event(item: dict[str, Any], repo: str, observed_at: datetime | None = None) -> Event:
    """One listing row becomes one observation.

    The `pull_request` key is how the issues endpoint marks a row as a PR. Its
    `merged_at` is the only merge signal available here, which is why a merged PR is
    recorded as state closed plus merged true rather than a state of its own.
    """
    pull = item.get("pull_request")
    payload = {
        "number": item["number"],
        "title": item.get("title") or "",
        "url": item.get("html_url") or "",
        "state": item.get("state") or "open",
        "labels": _labels(item),
        "assignees": [a["login"] for a in item.get("assignees") or [] if a.get("login")],
        "author": (item.get("user") or {}).get("login") or "",
        "author_association": item.get("author_association") or "NONE",
        "comments": item.get("comments") or 0,
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
    }
    if pull is not None:
        payload["draft"] = bool(item.get("draft"))
        payload["merged"] = bool(pull.get("merged_at"))

    return Event.make(
        kind=PR_OBSERVED if pull is not None else ISSUE_OBSERVED,
        repo=repo,
        subject=item["number"],
        # The observation is identified by the version of the thing it saw, so two
        # overlapping polls of an unchanged issue produce one row.
        occurred_at=item.get("updated_at"),
        payload=payload,
        observed_at=observed_at,
    )


def poll_repo(
    client: GitHubClient,
    full_name: str,
    cursor: Cursor,
    per_page: int = 100,
    now: datetime | None = None,
) -> tuple[PollResult, Cursor]:
    now = now or datetime.now(UTC)
    owner, name = split_name(full_name)
    watermark = cursor.watermark
    events: list[Event] = []
    seen: list[str | None] = []
    etag = cursor.etag
    pages = 0

    for page in range(1, MAX_PAGES + 1):
        path = LISTING.format(owner=owner, name=name, n=per_page)
        if page > 1:
            path = f"{path}&page={page}"

        try:
            # Only page one carries a conditional header: the ETag belongs to that URL.
            response = client.rest_conditional(path, etag if page == 1 else None)
        except NotFound:
            return PollResult(full_name, [], error="repository not found"), cursor
        except GitHubError as exc:
            return PollResult(full_name, [], error=str(exc)), cursor

        if response.unchanged:
            return (
                PollResult(full_name, [], unchanged=True),
                cursor.model_copy(
                    update={"last_polled": now, "quiet_polls": cursor.quiet_polls + 1}
                ),
            )

        if page == 1:
            etag = response.etag
        items = response.body or []
        pages += 1
        if not items:
            break

        stop = False
        for item in items:
            updated = item.get("updated_at")
            seen.append(updated)
            # Sorted by updated desc, so the first row at or before the watermark means
            # everything after it is already known. Compared inclusively on purpose -
            # a same-second collision costs one duplicate, which dedupe drops anyway.
            stamp = parse_time(updated) if updated else None
            if watermark is not None and stamp is not None and stamp <= watermark:
                stop = True
                break
            events.append(to_event(item, full_name, observed_at=now))

        if stop or len(items) < per_page:
            break

    stale_events, stale_etag = _walk_stalest(
        client, owner, name, cursor.stale_etag, per_page, full_name, now
    )
    events.extend(stale_events)

    return (
        PollResult(full_name, events, pages=pages, stale_seen=len(stale_events)),
        Cursor(
            etag=etag,
            stale_etag=stale_etag,
            watermark=newest(seen) or watermark,
            last_polled=now,
            quiet_polls=0,
        ),
    )


def _walk_stalest(
    client: GitHubClient,
    owner: str,
    name: str,
    etag: str | None,
    per_page: int,
    full_name: str,
    now: datetime,
) -> tuple[list[Event], str | None]:
    """One page from the far end of the list: the least recently touched open items.

    No watermark and no paging. The stalest page is a standing view of what has been
    abandoned, not a stream of changes, and re-observing the same rows is free - their
    ids are identical, so the log dedupes them away.
    """
    path = STALEST.format(owner=owner, name=name, n=per_page)
    try:
        response = client.rest_conditional(path, etag)
    except GitHubError:
        # A failure here must not lose the main walk, which has already succeeded.
        return [], etag
    if response.unchanged:
        return [], etag
    items = response.body or []
    return [to_event(item, full_name, observed_at=now) for item in items], response.etag


def poll_all(
    client: GitHubClient,
    repos: list[str],
    cursors: Cursors,
    per_page: int = 100,
    now: datetime | None = None,
) -> list[PollResult]:
    results = []
    for full_name in repos:
        result, cursor = poll_repo(client, full_name, cursors.get(full_name), per_page, now)
        if result.ok:
            cursors.set(full_name, cursor)
        results.append(result)
    return results
