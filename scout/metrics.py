"""Repository health, as numbers rather than impressions.

Star count tells you how many people watched a project. It tells you nothing about
whether that project will merge a patch from a stranger, which is the only question that
matters before you spend a weekend on it. Everything here is a pure function over
recorded API responses, so every number is testable without a network.

The load-bearing metric is `cold_merges`: pull requests merged in the lookback window
whose author had never contributed before. A repository with forty thousand stars and
zero cold merges in six months is a closed shop with a public issue tracker.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

# GitHub's authorAssociation, split by whether the person can merge.
MAINTAINER = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
# CONTRIBUTOR means "has landed something here before but is not on the team" - the state
# you are trying to reach, so it counts as an outsider win, not an insider one.
OUTSIDER = frozenset({"CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", "FIRST_TIMER", "NONE"})
# Never contributed here before. Whether these get merged is the whole question.
COLD = frozenset({"FIRST_TIME_CONTRIBUTOR", "FIRST_TIMER", "NONE"})

BEGINNER_LABELS = frozenset(
    {
        "good first issue",
        "good-first-issue",
        "beginner",
        "beginner friendly",
        "easy",
        "e-easy",
        "starter",
        "first-timers-only",
        "help wanted",
        "hacktoberfest",
    }
)

IST_OFFSET_HOURS = 5.5
# A working day peaks mid-afternoon. Used only to turn an observed activity peak into a
# human-readable timezone guess; the overlap fraction below does not depend on it.
ASSUMED_LOCAL_PEAK_HOUR = 14.0


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(UTC)


def _hours_between(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds() / 3600.0


def _circular_mean_hour(hours: Sequence[float]) -> float | None:
    """Mean of a clock, which a plain average gets wrong: 23:00 and 01:00 average to
    midnight, not noon. Returns None when activity is spread evenly enough that no peak
    exists, which is the honest answer for a project with contributors on every continent.
    """
    if len(hours) < 8:
        return None
    angles = [2 * math.pi * h / 24 for h in hours]
    x = sum(math.cos(a) for a in angles) / len(angles)
    y = sum(math.sin(a) for a in angles) / len(angles)
    if math.hypot(x, y) < 0.15:
        return None
    return (math.atan2(y, x) * 24 / (2 * math.pi)) % 24


@dataclass(frozen=True)
class Coverage:
    """What a fixed-size sample actually managed to look at.

    Every metric here is advertised over `lookback_days`, but the queries fetch a fixed
    count, newest first. On a busy repository that count runs out in days, so the number
    is real while the window it claims is not. Merged pull requests are worse: they are
    ordered by creation, so one opened long ago and merged last week falls outside the
    sample entirely. That drops slow merges and makes merge latency look better than it
    is, most severely on the large repositories where the question matters most.

    Reporting the span is how the card stops overstating what it measured. It is the same
    move as refusing to report a confident number from a sample that cannot support one.
    """

    requested: int
    returned: int
    span_days: float | None
    window_days: int

    @property
    def capped(self) -> bool:
        """The fetch limit was hit, so there is more the sample never saw. A caller that
        did not say what it asked for cannot claim to know, so it reports uncapped."""
        return self.requested > 0 and self.returned >= self.requested

    @property
    def fraction(self) -> float | None:
        if self.span_days is None:
            return None
        return min(self.span_days / self.window_days, 1.0)

    @property
    def partial(self) -> bool:
        """Capped and covering less than half the window: read the metric as recent-only."""
        return self.capped and (self.fraction or 0.0) < 0.5

    def describe(self) -> str:
        if self.returned == 0:
            return "no sample"
        if self.span_days is None or not self.capped:
            return f"{self.returned} rows, the whole {self.window_days}d window"
        return f"{self.returned} rows spanning {self.span_days:.0f}d of {self.window_days}d"


EMPTY_COVERAGE = Coverage(requested=0, returned=0, span_days=None, window_days=0)


def _coverage(
    raw: list[dict[str, Any]],
    used: list[dict[str, Any]],
    requested: int,
    window_days: int,
) -> Coverage:
    stamps = [s for n in used if (s := _parse(n.get("createdAt"))) is not None]
    span = (max(stamps) - min(stamps)).total_seconds() / 86400 if len(stamps) > 1 else None
    return Coverage(
        requested=requested,
        returned=len(raw),
        span_days=span,
        window_days=window_days,
    )


@dataclass(frozen=True)
class Opportunity:
    """A concrete thing to act on, found while probing. Not a metric - a work item."""

    kind: str
    number: int
    title: str
    url: str
    idle_days: int
    note: str = ""


@dataclass(frozen=True)
class RepoHealth:
    full_name: str
    description: str | None
    stars: int
    language: str | None
    license: str | None
    archived: bool
    is_fork: bool
    issues_enabled: bool
    days_since_push: int
    open_issues: int
    open_prs: int

    merged_sample: int
    outsider_merge_rate: float
    cold_merges: int
    median_days_to_merge_outsider: float | None
    merged_coverage: Coverage

    outsider_issues: int
    median_hours_to_maintainer_reply: float | None
    unanswered_outsider_issues: int

    beginner_contest_minutes: float | None
    beginner_issues_sampled: int
    issue_coverage: Coverage

    maintainer_peak_utc: float | None
    maintainer_utc_offset: float | None
    free_hour_overlap: float | None

    opportunities: list[Opportunity] = field(default_factory=list)

    @property
    def timezone_note(self) -> str:
        if self.maintainer_utc_offset is None:
            return "spread out - no single working day"
        offset = self.maintainer_utc_offset
        if -9 <= offset <= -4:
            return f"UTC{offset:+.0f} (Americas) - their morning is your evening"
        if -1 <= offset <= 3:
            return f"UTC{offset:+.0f} (Europe) - their whole day is your office hours"
        if 7 <= offset <= 10:
            return f"UTC{offset:+.0f} (East Asia) - overlaps your morning"
        return f"UTC{offset:+.0f}"


def _merge_metrics(
    nodes: list[dict[str, Any]], cutoff: datetime, requested: int, window_days: int
) -> dict[str, Any]:
    merged = [n for n in nodes if (m := _parse(n.get("mergedAt"))) is not None and m >= cutoff]
    coverage = _coverage(nodes, merged, requested, window_days)
    if not merged:
        return {
            "merged_sample": 0,
            "outsider_merge_rate": 0.0,
            "cold_merges": 0,
            "median_days_to_merge_outsider": None,
            "merged_coverage": coverage,
        }

    outsiders = [n for n in merged if n.get("authorAssociation") in OUTSIDER]
    cold = [n for n in merged if n.get("authorAssociation") in COLD]

    latencies = []
    for node in outsiders:
        created, merged_at = _parse(node.get("createdAt")), _parse(node.get("mergedAt"))
        if created and merged_at:
            latencies.append(_hours_between(created, merged_at) / 24.0)

    return {
        "merged_sample": len(merged),
        "outsider_merge_rate": len(outsiders) / len(merged),
        "cold_merges": len(cold),
        "median_days_to_merge_outsider": statistics.median(latencies) if latencies else None,
        "merged_coverage": coverage,
    }


def _issue_metrics(
    nodes: list[dict[str, Any]],
    cutoff: datetime,
    contest_cutoff: datetime,
    requested: int,
    window_days: int,
) -> dict[str, Any]:
    reply_hours: list[float] = []
    unanswered = 0
    outsider_issues = 0
    contest_minutes: list[float] = []
    beginner_sampled = 0
    maintainer_hours: list[float] = []
    used: list[dict[str, Any]] = []

    for node in nodes:
        created = _parse(node.get("createdAt"))
        if not created or created < cutoff:
            continue
        used.append(node)
        comments = node.get("comments", {}).get("nodes") or []

        for comment in comments:
            if comment.get("authorAssociation") in MAINTAINER and (
                stamp := _parse(comment.get("createdAt"))
            ):
                maintainer_hours.append(stamp.hour + stamp.minute / 60)

        if node.get("authorAssociation") in OUTSIDER:
            outsider_issues += 1
            first_reply = _first_comment_at(comments, MAINTAINER)
            if first_reply:
                reply_hours.append(_hours_between(created, first_reply))
            else:
                unanswered += 1

        labels = {
            (label.get("name") or "").lower()
            for label in (node.get("labels", {}).get("nodes") or [])
        }
        if labels & BEGINNER_LABELS and created >= contest_cutoff:
            beginner_sampled += 1
            # How long before somebody who is not a maintainer turns up to claim it.
            claim = _first_comment_at(comments, OUTSIDER)
            if claim:
                contest_minutes.append(_hours_between(created, claim) * 60)

    return {
        "outsider_issues": outsider_issues,
        "median_hours_to_maintainer_reply": (
            statistics.median(reply_hours) if reply_hours else None
        ),
        "unanswered_outsider_issues": unanswered,
        "beginner_contest_minutes": (
            statistics.median(contest_minutes) if contest_minutes else None
        ),
        "beginner_issues_sampled": beginner_sampled,
        "issue_coverage": _coverage(nodes, used, requested, window_days),
        "_maintainer_hours": maintainer_hours,
    }


def _first_comment_at(
    comments: list[dict[str, Any]], associations: frozenset[str]
) -> datetime | None:
    for comment in comments:
        if comment.get("authorAssociation") in associations and (
            stamp := _parse(comment.get("createdAt"))
        ):
            return stamp
    return None


def _timezone_metrics(
    maintainer_hours: list[float], free_hours: tuple[int, int]
) -> dict[str, Any]:
    peak = _circular_mean_hour(maintainer_hours)
    offset = None
    if peak is not None:
        offset = ((ASSUMED_LOCAL_PEAK_HOUR - peak + 12) % 24) - 12

    overlap = None
    if maintainer_hours:
        start, end = free_hours
        hits = 0
        for utc_hour in maintainer_hours:
            local = (utc_hour + IST_OFFSET_HOURS) % 24
            inside = start <= local < end if start < end else (local >= start or local < end)
            hits += int(inside)
        overlap = hits / len(maintainer_hours)

    return {
        "maintainer_peak_utc": peak,
        "maintainer_utc_offset": offset,
        "free_hour_overlap": overlap,
    }


def _opportunities(
    stale_data: dict[str, Any], now: datetime, assigned_days: int, pr_days: int
) -> list[Opportunity]:
    found: list[Opportunity] = []

    for node in stale_data.get("assigned", {}).get("nodes") or []:
        assignees = node.get("assignees", {}).get("nodes") or []
        updated = _parse(node.get("updatedAt"))
        if not assignees or not updated:
            continue
        idle = int(_hours_between(updated, now) / 24)
        if idle < assigned_days:
            continue
        # A cross-reference usually means a PR already points here. Not proof, but enough
        # to stop scout from suggesting you ask about work that is already in flight.
        if (node.get("crossRefs", {}).get("totalCount") or 0) > 0:
            continue
        who = ", ".join(a.get("login", "?") for a in assignees)
        found.append(
            Opportunity(
                kind="stale-assignment",
                number=node["number"],
                title=node.get("title", ""),
                url=node.get("url", ""),
                idle_days=idle,
                note=f"assigned to {who}, no linked PR",
            )
        )

    for node in stale_data.get("openPRs", {}).get("nodes") or []:
        updated = _parse(node.get("updatedAt"))
        if not updated or node.get("isDraft"):
            continue
        idle = int(_hours_between(updated, now) / 24)
        if idle < pr_days or node.get("authorAssociation") in MAINTAINER:
            continue
        author = (node.get("author") or {}).get("login", "?")
        found.append(
            Opportunity(
                kind="abandoned-pr",
                number=node["number"],
                title=node.get("title", ""),
                url=node.get("url", ""),
                idle_days=idle,
                note=f"opened by {author}",
            )
        )

    found.sort(key=lambda o: o.idle_days, reverse=True)
    return found


def build_health(
    overview: dict[str, Any],
    issues: dict[str, Any],
    stale: dict[str, Any],
    *,
    lookback_days: int,
    contest_window_days: int,
    stale_assignment_days: int,
    abandoned_pr_days: int,
    free_hours: tuple[int, int],
    merged_requested: int = 0,
    issues_requested: int = 0,
    now: datetime | None = None,
) -> RepoHealth:
    """Pure: three recorded API responses in, one health record out.

    `merged_requested` and `issues_requested` are the page sizes the caller asked GitHub
    for. They are not used in any metric - only to work out whether the sample hit that
    limit, which is what separates "this is the whole window" from "this is the last nine
    days of it".
    """
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=lookback_days)
    contest_cutoff = now - timedelta(days=contest_window_days)

    repo = overview["repository"]
    pushed = _parse(repo.get("pushedAt")) or now

    merge = _merge_metrics(
        repo.get("merged", {}).get("nodes") or [], cutoff, merged_requested, lookback_days
    )
    issue = _issue_metrics(
        issues["repository"]["issues"]["nodes"] or [],
        cutoff,
        contest_cutoff,
        issues_requested,
        lookback_days,
    )
    tz = _timezone_metrics(issue.pop("_maintainer_hours"), free_hours)

    return RepoHealth(
        full_name=repo["nameWithOwner"],
        description=repo.get("description"),
        stars=repo.get("stargazerCount", 0),
        language=(repo.get("primaryLanguage") or {}).get("name"),
        license=(repo.get("licenseInfo") or {}).get("spdxId"),
        archived=repo.get("isArchived", False),
        is_fork=repo.get("isFork", False),
        issues_enabled=repo.get("hasIssuesEnabled", True),
        days_since_push=int(_hours_between(pushed, now) / 24),
        open_issues=repo.get("openIssues", {}).get("totalCount", 0),
        open_prs=repo.get("openPRs", {}).get("totalCount", 0),
        opportunities=_opportunities(
            stale["repository"], now, stale_assignment_days, abandoned_pr_days
        ),
        **merge,
        **issue,
        **tz,
    )


# Verdicts, worst to best. The point of a verdict is to stop you researching a repository
# that has already answered the question.
DEAD, TRAP, THIN, VIABLE, GOOD = "DEAD", "TRAP", "THIN", "VIABLE", "GOOD"
RANK = {DEAD: 0, TRAP: 1, THIN: 2, VIABLE: 3, GOOD: 4}


def verdict(health: RepoHealth) -> tuple[str, list[str]]:
    if health.archived:
        return DEAD, ["archived"]
    if not health.issues_enabled:
        return DEAD, ["issues disabled"]
    if health.days_since_push > 120:
        return DEAD, [f"no push in {health.days_since_push} days"]

    fatal: list[str] = []
    if health.merged_sample >= 20 and health.cold_merges == 0:
        fatal.append(f"{health.merged_sample} PRs merged, none from a first-time contributor")
    if health.merged_sample >= 20 and health.outsider_merge_rate < 0.05:
        fatal.append(f"only {health.outsider_merge_rate:.0%} of merges came from outside")
    if (
        health.outsider_issues >= 10
        and health.unanswered_outsider_issues / health.outsider_issues > 0.8
    ):
        fatal.append("maintainers answer fewer than 1 in 5 outsider issues")
    if fatal:
        return TRAP, fatal

    if health.merged_sample < 10:
        return THIN, [f"only {health.merged_sample} merges in the window - too few to judge"]

    warn: list[str] = []
    if (reply := health.median_hours_to_maintainer_reply) is not None and reply > 336:
        warn.append(f"median {reply / 24:.0f} days to first maintainer reply")
    if (contest := health.beginner_contest_minutes) is not None and contest < 60:
        warn.append(f"beginner issues claimed in a median {contest:.0f} min - do not race")
    if (overlap := health.free_hour_overlap) is not None and overlap < 0.05:
        warn.append("maintainers are never active during your free hours")

    # Caveats describe how much of the window was seen. They never change the verdict:
    # cold_merges is a count, so five first-timers merged inside nine days is stronger
    # evidence than five across six months, not weaker. What a short window does distort
    # is the latency numbers, and that is what these say out loud.
    caveats = coverage_caveats(health)

    if health.cold_merges >= 3 and health.outsider_merge_rate >= 0.25 and not warn:
        return GOOD, [
            f"{health.cold_merges} first-timers merged, "
            f"{health.outsider_merge_rate:.0%} of merges from outside",
            *caveats,
        ]
    return VIABLE, [*(warn or ["nothing disqualifying"]), *caveats]


def coverage_caveats(health: RepoHealth) -> list[str]:
    """What the fixed page sizes stopped the probe from seeing."""
    notes: list[str] = []
    if health.merged_coverage.partial:
        span = health.merged_coverage.span_days or 0
        notes.append(
            f"merge sample covers {span:.0f}d, not "
            f"{health.merged_coverage.window_days}d - slow merges under-counted"
        )
    if health.issue_coverage.partial:
        span = health.issue_coverage.span_days or 0
        notes.append(
            f"issue sample covers {span:.0f}d, not {health.issue_coverage.window_days}d"
        )
    return notes


def sort_key(health: RepoHealth) -> tuple[int, float]:
    """Rank order for a list of probes: best verdict first, then openness to strangers."""
    return RANK[verdict(health)[0]], health.outsider_merge_rate


def rank(healths: Iterable[RepoHealth]) -> list[RepoHealth]:
    return sorted(healths, key=sort_key, reverse=True)
