"""GitHub client. GraphQL for everything that has a GraphQL shape.

One token means one 5000-point-per-hour budget shared by every probe, so the client
tracks what it spends and exposes it. Probing thirty repos should not be a surprise.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

from scout.config import get_settings
from scout.safety import assert_budget, assert_read_only

GRAPHQL_URL = "https://api.github.com/graphql"
REST_URL = "https://api.github.com"


class GitHubError(RuntimeError):
    """Any failure that is not worth retrying."""


class NotFound(GitHubError):
    pass


class RateLimited(GitHubError):
    def __init__(self, retry_after: float) -> None:
        self.retry_after = retry_after
        super().__init__(f"rate limited, retry in {retry_after:.0f}s")


@dataclass(frozen=True)
class ConditionalResponse:
    """A REST reply that may be a 304. `body` is None exactly when nothing changed."""

    status: int
    body: Any | None
    etag: str | None

    @property
    def unchanged(self) -> bool:
        return self.status == 304


class GitHubClient:
    def __init__(
        self,
        token: str | None = None,
        timeout: float | None = None,
        read_only: bool = True,
    ) -> None:
        settings = get_settings()
        # Read-only by default and everywhere in this phase. Turning it off is a
        # deliberate, greppable act, not something a refactor can do by accident.
        self.read_only = read_only
        self.rate_limit_floor = settings.rate_limit_floor
        self.token = token or settings.github_token
        if not self.token:
            raise GitHubError("no token - set SCOUT_GITHUB_TOKEN in .env")
        self._client = httpx.Client(
            timeout=timeout or settings.timeout_seconds,
            headers={
                "Authorization": f"bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "scout/0.1",
            },
        )
        self.points_spent = 0
        self.points_remaining: int | None = None
        # REST and GraphQL have separate budgets; a poller and a prober do not compete.
        self.rest_requests = 0
        self.rest_not_modified = 0
        self.rest_remaining: int | None = None

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> GitHubClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def graphql(self, query: str, **variables: Any) -> dict[str, Any]:
        """Run one GraphQL query. Retries transient 5xx and secondary rate limits."""
        payload = {"query": query, "variables": variables}
        if self.read_only:
            assert_read_only("POST", GRAPHQL_URL, query)
        for attempt in range(4):
            response = self._client.post(GRAPHQL_URL, json=payload)

            if response.status_code in (502, 503, 504):
                time.sleep(2**attempt)
                continue
            if response.status_code == 403:
                retry_after = float(response.headers.get("retry-after", 60))
                if attempt == 3:
                    raise RateLimited(retry_after)
                time.sleep(retry_after)
                continue
            if response.status_code == 401:
                raise GitHubError("token rejected - check SCOUT_GITHUB_TOKEN")
            response.raise_for_status()

            body = response.json()
            # GraphQL reports failure inside a 200. NOT_FOUND on a repo is a real answer
            # (renamed, deleted, or private), so it gets its own exception.
            if errors := body.get("errors"):
                types = {e.get("type") for e in errors}
                message = "; ".join(e.get("message", "?") for e in errors)
                if "NOT_FOUND" in types:
                    raise NotFound(message)
                if "RATE_LIMITED" in types:
                    raise RateLimited(60)
                raise GitHubError(message)

            data = body.get("data") or {}
            if limit := data.get("rateLimit"):
                self.points_spent += limit.get("cost", 0)
                self.points_remaining = limit.get("remaining")
            return data

        raise GitHubError("gave up after 4 attempts")

    def rest(self, path: str) -> Any | None:
        """REST fallback. Returns None for 404 rather than raising, because most callers
        are asking 'does this file exist' and absence is the answer, not an error."""
        response = self._client.get(f"{REST_URL}{path}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    def rest_conditional(self, path: str, etag: str | None = None) -> ConditionalResponse:
        """A REST GET that can come back 304.

        This is what makes polling nearly free: GitHub does not charge rate limit for a
        304, so an idle repository costs nothing to check. The catch is that an ETag is
        bound to the exact URL, so a caller must not put a moving `since=` parameter in
        the path - use a stable URL and stop reading at a watermark instead.

        GraphQL has no conditional-request equivalent, which is why polling uses REST
        while probing does not.
        """
        headers = {"If-None-Match": etag} if etag else {}
        for attempt in range(4):
            response = self._client.get(f"{REST_URL}{path}", headers=headers)
            self.rest_requests += 1

            if response.status_code in (502, 503, 504):
                time.sleep(2**attempt)
                continue
            if response.status_code == 403 and "rate limit" in response.text.lower():
                retry_after = float(response.headers.get("retry-after", 60))
                if attempt == 3:
                    raise RateLimited(retry_after)
                time.sleep(retry_after)
                continue
            if response.status_code == 401:
                raise GitHubError("token rejected - check SCOUT_GITHUB_TOKEN")
            if response.status_code == 404:
                raise NotFound(path)

            if remaining := response.headers.get("x-ratelimit-remaining"):
                self.rest_remaining = int(remaining)
                # Checked after the response so the floor is enforced on the *next* call:
                # stopping well short of zero keeps the token out of throttling, which
                # from GitHub's side is indistinguishable from abuse.
                assert_budget(self.rest_remaining, self.rate_limit_floor)

            if response.status_code == 304:
                self.rest_not_modified += 1
                return ConditionalResponse(status=304, body=None, etag=etag)

            response.raise_for_status()
            return ConditionalResponse(
                status=response.status_code,
                body=response.json(),
                etag=response.headers.get("etag"),
            )

        raise GitHubError(f"gave up on {path} after 4 attempts")
