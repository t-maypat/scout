"""Guards against the two ways this goes wrong: a surprise bill, and a banned account.

None of these are clever. They are cheap, boring checks placed where a mistake would
otherwise be silent - a loop that keeps polling after the rate limit is gone, a watchlist
that grew to two hundred repos, a refactor that quietly gives the poller write access.

The read-only guard is the important one. Scout's whole position is that it does not act
on GitHub without a human tap, and a comment posted by a bot is exactly the behaviour
that got open source communities gated in the first place. That promise should be
enforced by something that fails loudly, not by everyone remembering.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

# A GraphQL document that mutates. Matched loosely on purpose: a false positive costs one
# confused developer, a false negative costs a write nobody approved.
MUTATION = re.compile(r"\bmutation\b", re.IGNORECASE)

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
GRAPHQL_PATH = "/graphql"


class SafetyError(RuntimeError):
    """A refusal, not a failure. Scout stopped on purpose."""


class WriteAttempted(SafetyError):
    pass


class BudgetExhausted(SafetyError):
    pass


def assert_read_only(method: str, url: str, body: str = "") -> None:
    """Reject anything that could change state on GitHub.

    GraphQL queries are POSTs, so the method alone cannot decide this - the body has to
    be read. Everything else that is not a GET is refused outright.
    """
    method = method.upper()
    if method not in WRITE_METHODS:
        return
    if method == "POST" and url.endswith(GRAPHQL_PATH):
        if MUTATION.search(body):
            raise WriteAttempted(
                "refusing a GraphQL mutation: scout is read-only. Writing to GitHub is a "
                "deliberate act that belongs behind a human tap, not in a poller."
            )
        return
    raise WriteAttempted(
        f"refusing {method} {url}: scout is read-only. If this is intentional, the caller "
        "must construct its client with read_only=False and say why."
    )


def assert_enabled(enabled: bool) -> None:
    """The kill switch. One environment variable stops every scheduled job."""
    if not enabled:
        raise SafetyError("SCOUT_ENABLED is false - refusing to run")


def assert_repo_cap(repos: Sequence[str], cap: int) -> None:
    """A watchlist is meant to stay short. If it has grown past the cap, that is a signal
    the strategy drifted, not a reason to make a hundred API calls."""
    if cap > 0 and len(repos) > cap:
        raise SafetyError(
            f"refusing to poll {len(repos)} repositories (cap {cap}). Five you can build "
            f"beat fifty you cannot - raise SCOUT_MAX_POLL_REPOS only on purpose."
        )


def assert_budget(remaining: int | None, floor: int) -> None:
    """Stop well before the rate limit rather than at it.

    Hitting zero gets the token throttled and looks, from GitHub's side, exactly like
    abuse. Leaving a floor means an interactive `scout probe` still works after an
    automated run has been through.
    """
    if remaining is not None and remaining < floor:
        raise BudgetExhausted(
            f"only {remaining} rate-limit requests left (floor {floor}) - stopping. "
            "The budget resets hourly; nothing is lost."
        )
