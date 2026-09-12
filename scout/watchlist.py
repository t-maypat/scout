"""The watchlist: a TOML file you edit by hand and commit.

This is curated data, not configuration, which is why it is a file and not environment.
The whole premise of scout is that five repositories you can build beat five hundred you
cannot, so the list is meant to stay short enough to read in one screen.

`cold_start_minutes` is the field that decides whether an alert is actionable. A repo you
cannot get to a green test suite is a repo you cannot contribute to at 11pm, whatever the
issue tracker looks like.
"""

from __future__ import annotations

import tomllib
from datetime import UTC, datetime
from pathlib import Path

import tomli_w
from pydantic import BaseModel, Field

# candidate: probed, not yet built. building: you are fighting the toolchain.
# green: test suite passes locally, ready to act on. active: you have landed something.
# rejected: probed and ruled out, kept so scout never suggests it again.
STATUSES = ("candidate", "building", "green", "active", "rejected")


class WatchedRepo(BaseModel):
    full_name: str
    status: str = "candidate"
    why: str = ""
    # The toggle. False takes a repo out of the poll loop without losing what was learned
    # about it - useful while a project is in a noisy release week.
    poll: bool = True
    # Earns an interrupt rather than waiting for the evening digest. Keep this rare.
    hot: bool = False
    # Cold start. Empty until you have actually run them.
    setup: str = ""
    test: str = ""
    cold_start_minutes: int | None = None
    notes: str = ""
    # Last probe result, cached so `scout list` is instant and offline.
    last_probed: datetime | None = None
    verdict: str = ""
    verdict_reasons: list[str] = Field(default_factory=list)
    outsider_merge_rate: float | None = None
    cold_merges: int | None = None
    maintainer_utc_offset: float | None = None

    @property
    def actionable(self) -> bool:
        """Whether an alert about this repo is worth sending to your phone."""
        return self.status in ("green", "active")

    def should_poll(self, only_actionable: bool) -> bool:
        if not self.poll or self.status == "rejected":
            return False
        return self.actionable if only_actionable else True


class Watchlist(BaseModel):
    repo: list[WatchedRepo] = Field(default_factory=list)

    def find(self, full_name: str) -> WatchedRepo | None:
        target = full_name.lower()
        return next((r for r in self.repo if r.full_name.lower() == target), None)

    def upsert(self, entry: WatchedRepo) -> None:
        existing = self.find(entry.full_name)
        if existing is None:
            self.repo.append(entry)
            return
        self.repo[self.repo.index(existing)] = entry

    def remove(self, full_name: str) -> bool:
        existing = self.find(full_name)
        if existing is None:
            return False
        self.repo.remove(existing)
        return True


def path() -> Path:
    from scout.config import get_settings

    return Path(get_settings().watchlist_path)


def load(file: Path | None = None) -> Watchlist:
    file = file or path()
    if not file.exists():
        return Watchlist()
    return Watchlist.model_validate(tomllib.loads(file.read_text(encoding="utf-8")))


def save(watchlist: Watchlist, file: Path | None = None) -> None:
    file = file or path()
    payload = watchlist.model_dump(mode="json", exclude_none=True)
    header = (
        "# scout watchlist. Hand-edited and committed - this is curated data, not config.\n"
        "# status: " + " | ".join(STATUSES) + "\n"
        f"# last written {datetime.now(UTC).isoformat(timespec='seconds')}\n\n"
    )
    file.write_text(header + tomli_w.dumps(payload), encoding="utf-8")
