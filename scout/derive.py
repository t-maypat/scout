"""Derivation: replay observations into current state, transitions, and opportunities.

Everything here is disposable. Delete it, replay the log, get it back. Nothing in this
module reads the network or writes an event.

Bump DERIVATION_VERSION when the meaning of any output changes, then replay and diff.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from scout.events import ISSUE_OBSERVED, PR_OBSERVED, Event, parse_time
from scout.metrics import BEGINNER_LABELS, MAINTAINER, OUTSIDER

DERIVATION_VERSION = 1

ISSUE, PULL = "issue", "pr"


@dataclass
class Subject:
    """The latest known state of one issue or pull request."""

    repo: str
    number: int
    kind: str
    title: str = ""
    url: str = ""
    state: str = "open"
    draft: bool = False
    merged: bool = False
    labels: tuple[str, ...] = ()
    assignees: tuple[str, ...] = ()
    author: str = ""
    author_association: str = "NONE"
    comments: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None
    observations: int = 0

    @property
    def key(self) -> str:
        return f"{self.repo}#{self.number}"

    @property
    def is_open(self) -> bool:
        return self.state == "open"

    @property
    def by_outsider(self) -> bool:
        return self.author_association in OUTSIDER

    @property
    def beginner_labelled(self) -> bool:
        return bool({label.lower() for label in self.labels} & BEGINNER_LABELS)

    def idle_days(self, now: datetime) -> int:
        if not self.updated_at:
            return 0
        return int((now - self.updated_at).total_seconds() / 86400)

    def age_hours(self, now: datetime) -> float:
        if not self.created_at:
            return 0.0
        return (now - self.created_at).total_seconds() / 3600


@dataclass(frozen=True)
class Transition:
    """A change between two consecutive observations of the same subject.

    Derived, never logged. If it were logged, a correction to how a change is detected
    could not be replayed over history.
    """

    repo: str
    number: int
    kind: str
    what: str
    at: datetime
    detail: str = ""

    @property
    def key(self) -> str:
        return f"{self.repo}#{self.number}:{self.what}:{self.detail}"


@dataclass(frozen=True)
class Opportunity:
    """Work nobody is racing for, with the reason it qualifies."""

    repo: str
    number: int
    kind: str
    title: str
    url: str
    idle_days: int
    note: str
    subject_kind: str = ISSUE

    @property
    def key(self) -> str:
        return f"{self.repo}#{self.number}:{self.kind}"


@dataclass
class State:
    subjects: dict[str, Subject] = field(default_factory=dict)
    transitions: list[Transition] = field(default_factory=list)
    version: int = DERIVATION_VERSION

    def open_subjects(self, repo: str | None = None) -> list[Subject]:
        return [
            s
            for s in self.subjects.values()
            if s.is_open and (repo is None or s.repo == repo)
        ]


def _subject_from(payload: dict[str, Any], repo: str, kind: str) -> Subject:
    return Subject(
        repo=repo,
        number=int(payload["number"]),
        kind=kind,
        title=payload.get("title") or "",
        url=payload.get("url") or "",
        state=payload.get("state") or "open",
        draft=bool(payload.get("draft")),
        merged=bool(payload.get("merged")),
        labels=tuple(payload.get("labels") or ()),
        assignees=tuple(payload.get("assignees") or ()),
        author=payload.get("author") or "",
        author_association=payload.get("author_association") or "NONE",
        comments=int(payload.get("comments") or 0),
        created_at=parse_time(payload.get("created_at")),
        updated_at=parse_time(payload.get("updated_at")),
    )


def _diff(before: Subject, after: Subject) -> list[Transition]:
    """What changed between two observations. Only changes worth acting on."""
    at = after.updated_at or datetime.now(UTC)
    found: list[Transition] = []

    gained = set(after.assignees) - set(before.assignees)
    lost = set(before.assignees) - set(after.assignees)
    for who in sorted(gained):
        found.append(Transition(after.repo, after.number, after.kind, "assigned", at, who))
    for who in sorted(lost):
        # An issue losing its assignee is the cleanest opening there is: the work is
        # wanted, someone tried, and it is now unclaimed without a race.
        found.append(Transition(after.repo, after.number, after.kind, "unassigned", at, who))

    for label in sorted(set(after.labels) - set(before.labels)):
        found.append(Transition(after.repo, after.number, after.kind, "labeled", at, label))

    if before.state == "open" and after.state != "open":
        what = "merged" if after.merged else "closed"
        found.append(Transition(after.repo, after.number, after.kind, what, at))
    elif before.state != "open" and after.state == "open":
        found.append(Transition(after.repo, after.number, after.kind, "reopened", at))

    if before.draft and not after.draft:
        found.append(Transition(after.repo, after.number, after.kind, "ready_for_review", at))

    return found


def derive(events: Iterable[Event]) -> State:
    """Fold the log into current state. Pure, and safe to run on any prefix of the log."""
    ordered = sorted(
        (e for e in events if e.kind in (ISSUE_OBSERVED, PR_OBSERVED)),
        key=lambda e: (e.occurred_at or e.observed_at, e.id),
    )

    state = State()
    for event in ordered:
        kind = ISSUE if event.kind == ISSUE_OBSERVED else PULL
        try:
            observed = _subject_from(event.payload, event.repo, kind)
        except (KeyError, ValueError, TypeError):
            # A malformed row must not poison a replay of everything after it.
            continue

        previous = state.subjects.get(observed.key)
        if previous is not None:
            # Observations can arrive out of order across shards; an older snapshot must
            # never overwrite a newer one.
            stale = (
                previous.updated_at
                and observed.updated_at
                and observed.updated_at < previous.updated_at
            )
            if stale:
                continue
            state.transitions.extend(_diff(previous, observed))
            observed.observations = previous.observations + 1
        else:
            observed.observations = 1
        state.subjects[observed.key] = observed

    state.transitions.sort(key=lambda t: t.at)
    return state


def opportunities(
    state: State,
    *,
    now: datetime | None = None,
    stale_assignment_days: int = 21,
    abandoned_pr_days: int = 30,
    unanswered_min_hours: float = 24,
    unanswered_max_days: int = 14,
    repos: Iterable[str] | None = None,
) -> list[Opportunity]:
    """Uncontested work, computed from current state. Never from a live API call.

    These are deliberately not the beginner-labelled issues. Those are the most contested
    real estate on GitHub and the race for them is unwinnable from a different timezone.
    """
    now = now or datetime.now(UTC)
    allowed = set(repos) if repos is not None else None
    found: list[Opportunity] = []

    for subject in state.subjects.values():
        if not subject.is_open or (allowed is not None and subject.repo not in allowed):
            continue
        idle = subject.idle_days(now)

        if subject.kind == ISSUE and subject.assignees and idle >= stale_assignment_days:
            found.append(
                Opportunity(
                    repo=subject.repo,
                    number=subject.number,
                    kind="stale-assignment",
                    title=subject.title,
                    url=subject.url,
                    idle_days=idle,
                    note=f"assigned to {', '.join(subject.assignees)}, untouched",
                    subject_kind=ISSUE,
                )
            )

        if (
            subject.kind == PULL
            and not subject.draft
            and subject.author_association not in MAINTAINER
            and idle >= abandoned_pr_days
        ):
            found.append(
                Opportunity(
                    repo=subject.repo,
                    number=subject.number,
                    kind="abandoned-pr",
                    title=subject.title,
                    url=subject.url,
                    idle_days=idle,
                    note=f"opened by {subject.author}, no movement",
                    subject_kind=PULL,
                )
            )

        # An open bug report from an outsider that nobody has replied to. Reproducing it
        # and posting a minimal case is real work with no competition, and it is the
        # fastest way to become a name a maintainer recognises.
        age = subject.age_hours(now)
        if (
            subject.kind == ISSUE
            and subject.by_outsider
            and subject.comments == 0
            and not subject.assignees
            and not subject.beginner_labelled
            and unanswered_min_hours <= age <= unanswered_max_days * 24
        ):
            found.append(
                Opportunity(
                    repo=subject.repo,
                    number=subject.number,
                    kind="unanswered-report",
                    title=subject.title,
                    url=subject.url,
                    idle_days=int(age / 24),
                    note=f"reported by {subject.author}, no reply yet",
                    subject_kind=ISSUE,
                )
            )

    found.sort(key=lambda o: o.idle_days, reverse=True)
    return found


def recent_transitions(
    state: State,
    *,
    now: datetime | None = None,
    within_hours: float = 24,
    kinds: Iterable[str] = ("unassigned", "ready_for_review"),
) -> list[Transition]:
    """Changes worth an alert. `unassigned` is the headline: work that was wanted enough
    to be claimed, and is now free again without anyone racing for it."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(hours=within_hours)
    wanted = frozenset(kinds)
    return [t for t in state.transitions if t.what in wanted and t.at >= cutoff]
