"""Derivation: replay observations into current state, transitions, and opportunities.

Everything here is disposable. Delete it, replay the log, get it back. Nothing in this
module reads the network or writes an event.

Bump DERIVATION_VERSION when the meaning of any output changes, then replay and diff.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from scout.events import ISSUE_ENRICHED, ISSUE_OBSERVED, PR_OBSERVED, Event, parse_time
from scout.metrics import BEGINNER_LABELS, MAINTAINER

DERIVATION_VERSION = 2

# Somebody saying they are on it. Not a claim in any formal sense - GitHub has none
# outside assignment - but on most projects this comment *is* the claim, and walking into
# it is how a first contribution becomes a wasted evening. Matched loosely on purpose: a
# false positive costs an issue that was probably contested anyway, a false negative costs
# the whole point of the rule.
CLAIM = re.compile(
    r"\b("
    r"i.?ll (take|work|do|try|submit|open|have a go)"
    r"|i will (take|work|do|try|submit|open)"
    r"|can i (take|work|pick|have|try|give)"
    r"|could i (take|work|pick|try)"
    r"|may i (take|work|pick)"
    r"|assign (me|it to me|this to me)"
    r"|i.?m (working|on it|taking|looking into)"
    r"|i am (working|taking|looking into)"
    r"|working on (this|it)"
    r"|taking (this|it)"
    r"|picking (this|it) up"
    r"|i.?d like to (work|take|try|pick)"
    r"|pr (incoming|coming|on the way)"
    r"|raising a pr|opening a pr"
    r")\b",
    re.IGNORECASE,
)

# A maintainer has said the work is real and wanted. Either signal counts; the
# alternative is guessing from an untriaged title.
#
# `bug` is deliberately not here. On every large repository it is applied by the issue
# template the moment somebody files, so it says what the reporter clicked, not what a
# maintainer decided - the same mistake as trusting `authorAssociation`. Put it back
# through SCOUT_FRESH_ACCEPTING_LABELS for a project you know triages by hand.
# `help wanted` is also absent, for a different reason: it is in BEGINNER_LABELS, which
# this rule excludes. Maintainers use it to invite outside help, and bots race it for
# exactly that reason - so counting it here and excluding it there would contradict.
ACCEPTING_LABELS = frozenset(
    {
        "confirmed",
        "accepting prs",
        "accepting-prs",
        "pr welcome",
        "prs welcome",
        "pull requests welcome",
        "triaged",
        "ready",
        "ready for work",
    }
)

# Not work, or not yet work. A question is a conversation, a duplicate is closed by
# somebody else's fix, and needs-info is not actionable by anyone yet.
BLOCKING_LABELS = frozenset(
    {
        "question",
        "support",
        "duplicate",
        "invalid",
        "wontfix",
        "needs info",
        "needs-info",
        "needs more info",
        "needs reproduction",
        "stale",
        "discussion",
        "rfc",
        "blocked",
        "on hold",
    }
)

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
    # From enrichment, which asks about candidates only. `enriched_at` is the version of
    # the issue the answer describes: when it does not match `updated_at` the answer is
    # stale, and when it is None nobody asked. Both mean "unknown", never "nothing there"
    # - a rule that needs this data refuses rather than assumes.
    linked_prs: tuple[dict[str, Any], ...] = ()
    commit_refs: tuple[dict[str, Any], ...] = ()
    seen_comments: tuple[dict[str, Any], ...] = ()
    enriched_at: datetime | None = None
    # When the answer was fetched, as opposed to which version it describes. A commit
    # pushed to a fork does not touch the issue, so a matching version is not proof the
    # answer is current - `scout enrich` re-asks once this passes its TTL.
    enriched_observed_at: datetime | None = None

    @property
    def key(self) -> str:
        return f"{self.repo}#{self.number}"

    @property
    def is_open(self) -> bool:
        return self.state == "open"

    def by_maintainer(self, maintainers: frozenset[str]) -> bool:
        """The association is unreliable - it reports MEMBER only for publicly visible
        org membership - so the set the probe derived from who merged things wins."""
        return self.author_association in MAINTAINER or self.author in maintainers

    def by_outsider(self, maintainers: frozenset[str] = frozenset()) -> bool:
        return not self.by_maintainer(maintainers)

    @property
    def beginner_labelled(self) -> bool:
        return bool({label.lower() for label in self.labels} & BEGINNER_LABELS)

    @property
    def enriched(self) -> bool:
        """Is what we know about links and comments current for this version?"""
        return self.enriched_at is not None and self.enriched_at == self.updated_at

    @property
    def fork_commits(self) -> tuple[str, ...]:
        """Repositories other than this one holding a commit that names this issue.

        A commit in somebody else's fork is the earliest honest sign that an outsider has
        started - earlier than a pull request, which is the point. A commit in the
        upstream repository is usually a maintainer working or referring to it in passing,
        so it does not count as taken.
        """
        mine = self.repo.lower()
        return tuple(
            sorted(
                {
                    str(ref.get("repo"))
                    for ref in self.commit_refs
                    if ref.get("repo") and str(ref["repo"]).lower() != mine
                }
            )
        )

    @property
    def open_linked_prs(self) -> tuple[int, ...]:
        """Pull requests still open that name this issue. One is enough to walk away."""
        return tuple(
            int(pr["number"])
            for pr in self.linked_prs
            if str(pr.get("state", "")).upper() == "OPEN"
        )

    def claimants(self, maintainers: frozenset[str] = frozenset()) -> tuple[str, ...]:
        """Who has said they are on it, maintainers excluded - a maintainer writing "I
        will fix this" is them taking it, which the assignee check already covers, and
        "PRs welcome" from that same person is the opposite of a claim."""
        found = []
        for comment in self.seen_comments:
            author = comment.get("author") or ""
            association = comment.get("association") or "NONE"
            if association in MAINTAINER or author in maintainers:
                continue
            if CLAIM.search(comment.get("body") or ""):
                found.append(author)
        return tuple(dict.fromkeys(found))

    def maintainer_replied(self, maintainers: frozenset[str] = frozenset()) -> bool:
        return any(
            (c.get("association") or "NONE") in MAINTAINER
            or (c.get("author") or "") in maintainers
            for c in self.seen_comments
        )

    def accepting(self, accepted: frozenset[str] = ACCEPTING_LABELS) -> tuple[str, ...]:
        """Labels on this issue that mean a maintainer wants the work done."""
        lowered = {label.lower() for label in self.labels}
        return tuple(sorted(lowered & accepted))

    @property
    def blocked_by_label(self) -> bool:
        return bool({label.lower() for label in self.labels} & BLOCKING_LABELS)

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
    events = list(events)
    ordered = sorted(
        (e for e in events if e.kind in (ISSUE_OBSERVED, PR_OBSERVED)),
        key=lambda e: (e.occurred_at or e.observed_at, e.id),
    )
    # Enrichment describes one version of one issue, so the newest answer wins and is
    # attached after the fold: it is an answer *about* an observation, not one itself.
    enrichments: dict[str, Event] = {}
    for event in events:
        if event.kind != ISSUE_ENRICHED:
            continue
        key = f"{event.repo}#{event.subject}"
        best = enrichments.get(key)
        if best is None or (event.occurred_at or event.observed_at) >= (
            best.occurred_at or best.observed_at
        ):
            enrichments[key] = event

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

    for key, event in enrichments.items():
        subject = state.subjects.get(key)
        if subject is None:
            continue
        subject.linked_prs = tuple(event.payload.get("linked_prs") or ())
        subject.commit_refs = tuple(event.payload.get("commit_refs") or ())
        subject.seen_comments = tuple(event.payload.get("comments") or ())
        subject.enriched_at = event.occurred_at
        subject.enriched_observed_at = event.observed_at

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
    fresh_max_age_days: int = 7,
    accepting_labels: Iterable[str] | None = None,
    repos: Iterable[str] | None = None,
    maintainers: dict[str, Iterable[str]] | None = None,
) -> list[Opportunity]:
    """Uncontested work, computed from current state. Never from a live API call.

    These are deliberately not the beginner-labelled issues. Those are the most contested
    real estate on GitHub and the race for them is unwinnable from a different timezone.
    """
    now = now or datetime.now(UTC)
    allowed = set(repos) if repos is not None else None
    known = {repo: frozenset(logins) for repo, logins in (maintainers or {}).items()}
    accepted = (
        ACCEPTING_LABELS
        if accepting_labels is None
        else frozenset(label.lower() for label in accepting_labels)
    )
    found: list[Opportunity] = []

    for subject in state.subjects.values():
        if not subject.is_open or (allowed is not None and subject.repo not in allowed):
            continue
        team = known.get(subject.repo, frozenset())
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
            and not subject.by_maintainer(team)
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
            and subject.by_outsider(team)
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

        # Fresh and free: young, unassigned, no pull request open against it, nobody in
        # the comments saying they are on it, and a maintainer has shown it is real work.
        # The only rule that needs enrichment - without it the answer is unknown rather
        # than yes, so an unenriched issue is never offered.
        if (
            subject.kind == ISSUE
            and subject.enriched
            and not subject.assignees
            and not subject.beginner_labelled
            and not subject.blocked_by_label
            and not subject.open_linked_prs
            and not subject.fork_commits
            and not subject.claimants(team)
            and age <= fresh_max_age_days * 24
            and (subject.maintainer_replied(team) or subject.accepting(accepted))
        ):
            why = (
                f"labelled {', '.join(subject.accepting(accepted))}"
                if subject.accepting(accepted)
                else "a maintainer replied"
            )
            found.append(
                Opportunity(
                    repo=subject.repo,
                    number=subject.number,
                    kind="fresh-and-free",
                    title=subject.title,
                    url=subject.url,
                    idle_days=int(age / 24),
                    note=f"{why}, no PR, nobody claiming it",
                    subject_kind=ISSUE,
                )
            )

    # Fresh first and youngest first, because those are the ones that stop being free.
    # Everything else is ranked by how long it has sat, which only grows.
    found.sort(
        key=lambda o: (0, o.idle_days) if o.kind == "fresh-and-free" else (1, -o.idle_days)
    )
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
