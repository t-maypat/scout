"""The event log: append-only JSONL, sharded by month, committed to git.

Git is already an append-only log with history and durability, so the asset lives there
rather than in a database. Derived state is disposable and rebuilt by replaying this.

One thing worth being honest about up front: **GitHub's API returns state, not events.**
A poll tells you what an issue looks like now, not what happened to it. So what gets
logged here are *observations* - a snapshot of one subject at one moment - and every
transition ("newly assigned", "went stale") is derived by comparing consecutive
observations. Recording transitions directly would put conclusions in the log, and then
a fix to how a conclusion is drawn could not be replayed.

Event ids are deterministic: the same observation seen by two overlapping polls produces
the same id and is written once. Running the poller twice writes nothing the second time.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Observations - what GitHub told us a thing looked like.
ISSUE_OBSERVED = "issue_observed"
PR_OBSERVED = "pr_observed"
# Our own actions, logged so they can be replayed and so a digest never repeats itself.
PROBE_COMPLETED = "probe_completed"
NOTIFICATION_SENT = "notification_sent"
# What the listing endpoint cannot say: whether a pull request is already linked to an
# issue, and who has spoken up in its comments. Recorded as raw as is practical - the
# rules that read "claimed" or "validated" out of it belong in derivation, so they can be
# fixed and replayed.
ISSUE_ENRICHED = "issue_enriched"

KINDS = frozenset(
    {ISSUE_OBSERVED, PR_OBSERVED, PROBE_COMPLETED, NOTIFICATION_SENT, ISSUE_ENRICHED}
)


def _now() -> datetime:
    # Second precision, matching what to_json writes. Keeping microseconds in memory that
    # the log cannot hold would make an event unequal to itself after a round trip.
    return datetime.now(UTC).replace(microsecond=0)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


@dataclass(frozen=True)
class Event:
    id: str
    kind: str
    repo: str
    subject: str
    occurred_at: datetime | None
    observed_at: datetime
    payload: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def make(
        cls,
        kind: str,
        repo: str,
        subject: str | int,
        occurred_at: datetime | str | None,
        payload: dict[str, Any] | None = None,
        observed_at: datetime | None = None,
    ) -> Event:
        if kind not in KINDS:
            raise ValueError(f"unknown event kind {kind!r}")
        if isinstance(occurred_at, str):
            occurred_at = parse_time(occurred_at)
        subject = str(subject)
        # The identity of an observation is what it observed, not when we happened to
        # look. Two polls that both see version N of issue 42 write one row.
        digest = hashlib.sha256(
            "\x1f".join([kind, repo, subject, _iso(occurred_at) or ""]).encode()
        ).hexdigest()[:16]
        return cls(
            id=digest,
            kind=kind,
            repo=repo,
            subject=subject,
            occurred_at=occurred_at,
            observed_at=(observed_at or _now()).replace(microsecond=0),
            payload=payload or {},
        )

    def to_json(self) -> str:
        return json.dumps(
            {
                "id": self.id,
                "kind": self.kind,
                "repo": self.repo,
                "subject": self.subject,
                "occurred_at": _iso(self.occurred_at),
                "observed_at": _iso(self.observed_at),
                "payload": self.payload,
            },
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, line: str) -> Event:
        raw = json.loads(line)
        return cls(
            id=raw["id"],
            kind=raw["kind"],
            repo=raw["repo"],
            subject=raw["subject"],
            occurred_at=parse_time(raw.get("occurred_at")),
            observed_at=parse_time(raw["observed_at"]) or _now(),
            payload=raw.get("payload") or {},
        )


class EventLog:
    """Append-only, sharded by the month the observation was recorded.

    Deduplication scans the log. At the volume this runs at - roughly ten repositories
    producing tens of events a day - that is a few megabytes a year and a scan costs
    milliseconds. If it ever stops being cheap, the fix is an id index beside the shards,
    not a database.
    """

    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)
        self._ids: set[str] | None = None

    def shard_for(self, when: datetime) -> Path:
        return self.directory / f"{when:%Y-%m}.jsonl"

    def shards(self) -> list[Path]:
        if not self.directory.exists():
            return []
        return sorted(self.directory.glob("*.jsonl"))

    def known_ids(self) -> set[str]:
        if self._ids is None:
            self._ids = {event.id for event in self.read()}
        return self._ids

    def read(self, kinds: Iterable[str] | None = None, repo: str | None = None) -> Iterator[Event]:
        wanted = frozenset(kinds) if kinds else None
        for shard in self.shards():
            with shard.open(encoding="utf-8") as handle:
                for line in handle:
                    if not (line := line.strip()):
                        continue
                    event = Event.from_json(line)
                    if wanted and event.kind not in wanted:
                        continue
                    if repo and event.repo != repo:
                        continue
                    yield event

    def append(self, events: Iterable[Event]) -> list[Event]:
        """Write the events not already present. Returns what was actually written, so a
        caller can say '0 new' rather than guessing."""
        known = self.known_ids()
        fresh: list[Event] = []
        seen_here: set[str] = set()
        for event in events:
            if event.id in known or event.id in seen_here:
                continue
            seen_here.add(event.id)
            fresh.append(event)

        if not fresh:
            return []

        self.directory.mkdir(parents=True, exist_ok=True)
        by_shard: dict[Path, list[Event]] = {}
        for event in fresh:
            by_shard.setdefault(self.shard_for(event.observed_at), []).append(event)
        for shard, batch in by_shard.items():
            with shard.open("a", encoding="utf-8") as handle:
                for event in batch:
                    handle.write(event.to_json() + "\n")

        known.update(event.id for event in fresh)
        return fresh

    def count(self) -> int:
        return len(self.known_ids())
