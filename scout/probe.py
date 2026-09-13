"""Run the queries against one repository and hand the responses to the scorer.

Pagination matters more than it looks. A fixed hundred-row page is plenty for a quiet
project and useless for a busy one: on a repository merging fifty pull requests a day,
one page covers two days of a hundred-and-eighty-day window, and the absence of
first-time contributors in those two days says nothing at all. So the prober keeps
fetching until the window is covered or the page budget runs out, and reports which.

Cost is roughly one rate-limit point per page. The default walk is cheap; a deep probe of
a very fast repository might spend thirty, against five thousand an hour.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from scout import queries
from scout.config import Settings, get_settings
from scout.github import GitHubClient
from scout.metrics import RepoHealth, build_health

# Each page is one round trip. A repository that cannot be covered in this many is fast
# enough that the recent window is genuinely representative of how it behaves now.
DEFAULT_MAX_PAGES = 4


def split_name(full_name: str) -> tuple[str, str]:
    """Accept owner/repo, a GitHub URL, or a git remote, and return (owner, name)."""
    text = full_name.strip().removesuffix(".git")
    for prefix in ("https://github.com/", "http://github.com/", "git@github.com:"):
        text = text.removeprefix(prefix)
    parts = [p for p in text.split("/") if p]
    if len(parts) < 2:
        raise ValueError(f"cannot read {full_name!r} as owner/repo")
    return parts[0], parts[1]


def _oldest_created(nodes: list[dict[str, Any]]) -> datetime | None:
    stamps = []
    for node in nodes:
        if raw := node.get("createdAt"):
            stamps.append(datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC))
    return min(stamps) if stamps else None


def _walk(
    client: GitHubClient,
    document: str,
    variables: dict[str, Any],
    path: tuple[str, ...],
    cutoff: datetime,
    max_pages: int,
) -> tuple[list[dict[str, Any]], dict[str, Any], int]:
    """Page until the lookback window is covered, the data runs out, or the budget is hit.

    Returns the accumulated nodes, the first page's full response (which carries the
    repository metadata), and how many pages were actually fetched.
    """
    collected: list[dict[str, Any]] = []
    first_response: dict[str, Any] = {}
    cursor: str | None = None
    pages = 0

    for page in range(max_pages):
        data = client.graphql(document, **variables, after=cursor)
        if page == 0:
            first_response = data
        pages += 1

        connection: Any = data
        for key in path:
            connection = (connection or {}).get(key) or {}
        nodes = connection.get("nodes") or []
        collected.extend(nodes)

        # Ordered newest-first, so once the oldest row on this page predates the window
        # there is nothing further back worth asking for.
        oldest = _oldest_created(nodes)
        if oldest is not None and oldest <= cutoff:
            break
        info = connection.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            break
        cursor = info.get("endCursor")

    return collected, first_response, pages


def probe(
    client: GitHubClient,
    full_name: str,
    settings: Settings | None = None,
    now: datetime | None = None,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> RepoHealth:
    settings = settings or get_settings()
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=settings.lookback_days)
    owner, name = split_name(full_name)

    merged_nodes, overview, merged_pages = _walk(
        client,
        queries.OVERVIEW,
        {"owner": owner, "name": name, "prs": settings.merged_pr_sample},
        ("repository", "merged"),
        cutoff,
        max_pages,
    )
    issue_nodes, issues_first, issue_pages = _walk(
        client,
        queries.ISSUES,
        {"owner": owner, "name": name, "n": settings.issue_sample},
        ("repository", "issues"),
        cutoff,
        max_pages,
    )
    stale = client.graphql(
        queries.STALE, owner=owner, name=name, n=settings.issue_sample, after=None
    )

    # Rebuild the payloads the scorer expects, now holding every page that was walked.
    overview["repository"]["merged"]["nodes"] = merged_nodes
    issues_first["repository"]["issues"]["nodes"] = issue_nodes

    return build_health(
        overview,
        issues_first,
        stale,
        lookback_days=settings.lookback_days,
        contest_window_days=settings.contest_window_days,
        stale_assignment_days=settings.stale_assignment_days,
        abandoned_pr_days=settings.abandoned_pr_days,
        free_hours=settings.free_hours,
        # What was actually asked for across every page, so `capped` still means "the
        # budget ran out", not "one page was full".
        merged_requested=settings.merged_pr_sample * merged_pages,
        issues_requested=settings.issue_sample * issue_pages,
        unanswered_after_hours=settings.unanswered_after_hours,
        now=now,
    )
