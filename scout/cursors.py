"""Poll bookmarks: per-repo ETag and watermark.

Deliberately not events. An ETag is not a fact about the world, it is a note about where
this machine got to, and replaying the log must not depend on it. Delete this file and
the next poll simply re-reads a page it has already seen - the event ids dedupe it away.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from scout.events import parse_time


class Cursor(BaseModel):
    etag: str | None = None
    # The stalest-first pass has its own url, so its own ETag. It almost always comes
    # back 304, because by definition nothing there is moving.
    stale_etag: str | None = None
    watermark: datetime | None = None
    last_polled: datetime | None = None
    # Consecutive 304s. Useful for backing off repos that never change.
    quiet_polls: int = 0


class Cursors(BaseModel):
    repo: dict[str, Cursor] = Field(default_factory=dict)

    def get(self, full_name: str) -> Cursor:
        return self.repo.get(full_name, Cursor())

    def set(self, full_name: str, cursor: Cursor) -> None:
        self.repo[full_name] = cursor


def path() -> Path:
    from scout.config import get_settings

    return Path(get_settings().state_dir) / "cursors.json"


def load(file: Path | None = None) -> Cursors:
    file = file or path()
    if not file.exists():
        return Cursors()
    raw = json.loads(file.read_text(encoding="utf-8"))
    # Hand-edited or truncated files should cost one wasted poll, not a crash.
    try:
        return Cursors.model_validate(raw)
    except Exception:
        return Cursors()


def save(cursors: Cursors, file: Path | None = None) -> None:
    file = file or path()
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(
        cursors.model_dump_json(indent=2, exclude_none=True) + "\n", encoding="utf-8"
    )


def newest(values: list[str | None]) -> datetime | None:
    stamps = [s for v in values if (s := parse_time(v)) is not None]
    return max(stamps) if stamps else None
