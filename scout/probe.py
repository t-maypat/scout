"""Run the three queries against one repository and hand the responses to the scorer.

Three round trips per repository, roughly 3 rate-limit points. A thirty-repo refresh
costs about 90 of the 5000 points an hour, so refreshing the whole watchlist hourly is
comfortably free.
"""

from __future__ import annotations

from datetime import datetime

from scout import queries
from scout.config import Settings, get_settings
from scout.github import GitHubClient
from scout.metrics import RepoHealth, build_health


def split_name(full_name: str) -> tuple[str, str]:
    """Accept owner/repo, a GitHub URL, or a git remote, and return (owner, name)."""
    text = full_name.strip().removesuffix(".git")
    for prefix in ("https://github.com/", "http://github.com/", "git@github.com:"):
        text = text.removeprefix(prefix)
    parts = [p for p in text.split("/") if p]
    if len(parts) < 2:
        raise ValueError(f"cannot read {full_name!r} as owner/repo")
    return parts[0], parts[1]


def probe(
    client: GitHubClient,
    full_name: str,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> RepoHealth:
    settings = settings or get_settings()
    owner, name = split_name(full_name)

    overview = client.graphql(
        queries.OVERVIEW, owner=owner, name=name, prs=settings.merged_pr_sample
    )
    issues = client.graphql(queries.ISSUES, owner=owner, name=name, n=settings.issue_sample)
    stale = client.graphql(queries.STALE, owner=owner, name=name, n=settings.issue_sample)

    return build_health(
        overview,
        issues,
        stale,
        lookback_days=settings.lookback_days,
        contest_window_days=settings.contest_window_days,
        stale_assignment_days=settings.stale_assignment_days,
        abandoned_pr_days=settings.abandoned_pr_days,
        free_hours=settings.free_hours,
        merged_requested=settings.merged_pr_sample,
        issues_requested=settings.issue_sample,
        now=now,
    )
