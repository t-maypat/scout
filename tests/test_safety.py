"""The guards. Each of these exists because the failure it prevents is silent."""

from __future__ import annotations

import pytest

from scout import queries, safety
from scout.github import GRAPHQL_URL


class TestReadOnly:
    """Scout's whole position is that it does not act on GitHub without a human tap.
    A bot posting comments is the exact behaviour that got communities gated."""

    def test_a_graphql_query_is_allowed(self):
        safety.assert_read_only("POST", GRAPHQL_URL, queries.OVERVIEW)

    def test_every_shipped_query_passes_the_guard(self):
        for document in (queries.OVERVIEW, queries.ISSUES, queries.STALE):
            safety.assert_read_only("POST", GRAPHQL_URL, document)

    def test_a_graphql_mutation_is_refused(self):
        mutation = "mutation AddComment($id: ID!) { addComment(input: {subjectId: $id}) }"
        with pytest.raises(safety.WriteAttempted, match="read-only"):
            safety.assert_read_only("POST", GRAPHQL_URL, mutation)

    def test_mutation_detection_is_case_insensitive(self):
        with pytest.raises(safety.WriteAttempted):
            safety.assert_read_only("POST", GRAPHQL_URL, "MUTATION Foo { x }")

    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
    def test_rest_writes_are_refused(self, method):
        url = "https://api.github.com/repos/acme/widget/issues/1/comments"
        with pytest.raises(safety.WriteAttempted, match="read-only"):
            safety.assert_read_only(method, url, '{"body": "I would like to work on this"}')

    def test_gets_pass(self):
        safety.assert_read_only("GET", "https://api.github.com/repos/acme/widget/issues")

    def test_a_post_to_something_merely_ending_in_graphql_like_text_is_still_checked(self):
        """The allowance is for the GraphQL endpoint, not for any url mentioning it."""
        with pytest.raises(safety.WriteAttempted):
            safety.assert_read_only("POST", "https://api.github.com/graphql/../repos", "")


class TestBudget:
    def test_stops_short_of_the_limit(self):
        with pytest.raises(safety.BudgetExhausted, match="stopping"):
            safety.assert_budget(remaining=100, floor=500)

    def test_allows_a_healthy_budget(self):
        safety.assert_budget(remaining=4800, floor=500)

    def test_unknown_budget_is_not_treated_as_empty(self):
        safety.assert_budget(remaining=None, floor=500)

    def test_the_floor_leaves_room_for_an_interactive_probe(self):
        """A scheduled run must not consume the budget an interactive command needs."""
        safety.assert_budget(remaining=501, floor=500)
        with pytest.raises(safety.BudgetExhausted):
            safety.assert_budget(remaining=499, floor=500)


class TestKillSwitch:
    def test_disabled_refuses(self):
        with pytest.raises(safety.SafetyError, match="SCOUT_ENABLED"):
            safety.assert_enabled(False)

    def test_enabled_proceeds(self):
        safety.assert_enabled(True)


class TestRepoCap:
    def test_an_overgrown_watchlist_is_refused(self):
        with pytest.raises(safety.SafetyError, match="cap 25"):
            safety.assert_repo_cap([f"acme/repo{n}" for n in range(26)], cap=25)

    def test_a_short_watchlist_passes(self):
        safety.assert_repo_cap(["acme/widget"], cap=25)

    def test_a_zero_cap_means_no_cap(self):
        safety.assert_repo_cap([f"acme/repo{n}" for n in range(500)], cap=0)


class TestClientIsReadOnlyByDefault:
    def test_the_default_client_refuses_a_mutation(self, monkeypatch):
        from scout.github import GitHubClient

        monkeypatch.setenv("SCOUT_GITHUB_TOKEN", "ghp_test")
        from scout.config import get_settings

        get_settings.cache_clear()
        client = GitHubClient(token="ghp_test")
        assert client.read_only is True
        with pytest.raises(safety.WriteAttempted):
            client.graphql("mutation { addComment(input: {}) { clientMutationId } }")
        client.close()
        get_settings.cache_clear()
