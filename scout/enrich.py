"""Enrichment: the two things the issues listing cannot tell you.

`/issues` says who is assigned and how many comments there are. It does not say whether
somebody has already opened a pull request for an issue, or whether the comments are
three people asking to be assigned. Both decide whether an issue is actually free, so
without them "fresh and free" is a guess.

This runs over candidates only - open issues, young, unassigned - so it costs one GraphQL
request per batch of ten, a handful a day, rather than a request per issue.

What it writes is deliberately close to raw: the linked pull requests, and each comment's
author, association and opening words. Reading "claimed" or "validated" out of that is
derivation's job, so a mistake in those rules can be fixed and replayed over the log
rather than being baked into what was recorded.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from typing import Any

from scout.derive import ISSUE, State, Subject
from scout.events import ISSUE_ENRICHED, Event
from scout.github import GitHubClient, GitHubError
from scout.probe import split_name

# Enough of a comment to recognise a claim ("can I take this?") without turning the event
# log into a copy of the conversation.
BODY_CHARS = 280
# Both ends of the timeline that can name a pull request. A CONNECTED_EVENT is somebody
# linking one explicitly; a CROSS_REFERENCED_EVENT is a PR that mentions the issue.
CANDIDATE = """
fragment Candidate on Issue {
  number
  updatedAt
  timelineItems(last: 50, itemTypes: [CROSS_REFERENCED_EVENT, CONNECTED_EVENT]) {
    nodes {
      __typename
      ... on CrossReferencedEvent { source { ... on PullRequest { number state isDraft } } }
      ... on ConnectedEvent { subject { ... on PullRequest { number state isDraft } } }
    }
  }
  comments(last: 30) {
    nodes { createdAt authorAssociation author { login } body }
  }
}
"""


def document(numbers: Iterable[int]) -> str:
    """One query for a batch of issues, each aliased by its number."""
    fields = "\n".join(f"    i{n}: issue(number: {n}) {{ ...Candidate }}" for n in numbers)
    return (
        "query($owner: String!, $name: String!) {\n"
        "  rateLimit { cost remaining }\n"
        "  repository(owner: $owner, name: $name) {\n"
        f"{fields}\n"
        "  }\n"
        "}\n" + CANDIDATE
    )


def candidates(
    state: State,
    *,
    now: datetime | None = None,
    max_age_days: int = 7,
    limit: int = 40,
    repos: Iterable[str] | None = None,
) -> list[Subject]:
    """Open, young, unassigned issues whose current version has not been enriched yet.

    Deliberately wider than the opportunity rule: this decides what is worth asking about,
    not what is worth sending. Issues already carrying an answer for their current version
    are skipped, so a quiet day costs nothing.
    """
    now = now or datetime.now(UTC)
    allowed = set(repos) if repos is not None else None
    found = [
        subject
        for subject in state.subjects.values()
        if subject.kind == ISSUE
        and subject.is_open
        and not subject.assignees
        and (allowed is None or subject.repo in allowed)
        and subject.age_hours(now) <= max_age_days * 24
        and subject.enriched_at != subject.updated_at
    ]
    # Youngest first: if the budget runs out, it runs out on the issues least likely to
    # still be free tomorrow.
    found.sort(key=lambda s: s.created_at or now, reverse=True)
    return found[:limit]


def batched(subjects: list[Subject], size: int) -> Iterator[list[Subject]]:
    for start in range(0, len(subjects), size):
        yield subjects[start : start + size]


def _pull_requests(node: dict[str, Any]) -> list[dict[str, Any]]:
    """Every pull request the timeline names, newest mention last, one row each."""
    seen: dict[int, dict[str, Any]] = {}
    for item in (node.get("timelineItems") or {}).get("nodes") or []:
        pull = item.get("source") or item.get("subject") or {}
        number = pull.get("number")
        if not number:
            continue
        seen[int(number)] = {
            "number": int(number),
            "state": pull.get("state") or "",
            "draft": bool(pull.get("isDraft")),
        }
    return [seen[n] for n in sorted(seen)]


def _comments(node: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "author": (comment.get("author") or {}).get("login") or "",
            "association": comment.get("authorAssociation") or "NONE",
            "created_at": comment.get("createdAt"),
            "body": (comment.get("body") or "")[:BODY_CHARS],
        }
        for comment in (node.get("comments") or {}).get("nodes") or []
    ]


def to_event(
    repo: str, node: dict[str, Any], updated_at: datetime | None, observed_at: datetime
) -> Event:
    """One enriched issue becomes one observation.

    Identified by the version of the issue it describes, exactly like a poll observation,
    so enriching an unchanged issue twice writes one row.
    """
    return Event.make(
        kind=ISSUE_ENRICHED,
        repo=repo,
        subject=node["number"],
        occurred_at=updated_at,
        payload={
            "number": int(node["number"]),
            "linked_prs": _pull_requests(node),
            "comments": _comments(node),
        },
        observed_at=observed_at,
    )


def enrich(
    client: GitHubClient,
    state: State,
    *,
    now: datetime | None = None,
    max_age_days: int = 7,
    limit: int = 40,
    batch_size: int = 10,
    repos: Iterable[str] | None = None,
) -> tuple[list[Event], list[str]]:
    """Ask about every candidate. Returns the events and whatever went wrong, named.

    A failure on one repository must not lose the batches that already succeeded: the
    events are returned either way, and the caller appends them.
    """
    now = now or datetime.now(UTC)
    wanted = candidates(
        state, now=now, max_age_days=max_age_days, limit=limit, repos=repos
    )
    by_repo: dict[str, list[Subject]] = {}
    for subject in wanted:
        by_repo.setdefault(subject.repo, []).append(subject)

    events: list[Event] = []
    problems: list[str] = []
    for repo, subjects in by_repo.items():
        owner, name = split_name(repo)
        for batch in batched(subjects, batch_size):
            updated = {s.number: s.updated_at for s in batch}
            try:
                data = client.graphql(
                    document(s.number for s in batch), owner=owner, name=name
                )
            except GitHubError as exc:
                problems.append(f"{repo}: {exc}")
                continue
            repository = data.get("repository") or {}
            for node in repository.values():
                # `rateLimit` sits beside the aliases; only the issues have numbers.
                if not isinstance(node, dict) or "number" not in node:
                    continue
                number = int(node["number"])
                events.append(to_event(repo, node, updated.get(number), now))
    return events, problems
