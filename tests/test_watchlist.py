"""Watchlist round-trips. The file is committed and hand-edited, so it has to survive
being written by scout and read back by a person without losing anything.
"""

from __future__ import annotations

from datetime import UTC, datetime

from scout import watchlist
from scout.probe import split_name


def test_round_trip_preserves_every_field(tmp_path):
    entry = watchlist.WatchedRepo(
        full_name="acme/widget",
        status="green",
        why="I use it daily and the maintainers reply",
        setup="uv sync",
        test="uv run pytest",
        cold_start_minutes=12,
        notes="needs postgres on 5433",
        last_probed=datetime(2026, 9, 13, 6, 30, tzinfo=UTC),
        verdict="GOOD",
        verdict_reasons=["6 first-timers merged"],
        outsider_merge_rate=0.44,
        cold_merges=6,
        maintainer_utc_offset=-7.0,
    )
    book = watchlist.Watchlist(repo=[entry])
    file = tmp_path / "watchlist.toml"
    watchlist.save(book, file)

    back = watchlist.load(file)
    assert back.repo == [entry]


def test_missing_file_is_an_empty_list_not_an_error(tmp_path):
    assert watchlist.load(tmp_path / "nothing.toml").repo == []


def test_upsert_replaces_rather_than_duplicating(tmp_path):
    book = watchlist.Watchlist(repo=[watchlist.WatchedRepo(full_name="acme/widget")])
    book.upsert(watchlist.WatchedRepo(full_name="acme/widget", status="green"))
    assert len(book.repo) == 1
    assert book.repo[0].status == "green"


def test_lookup_is_case_insensitive():
    book = watchlist.Watchlist(repo=[watchlist.WatchedRepo(full_name="Acme/Widget")])
    assert book.find("acme/widget") is not None


def test_only_buildable_repos_are_worth_an_alert():
    """The whole filter: an alert about a repo you cannot build wastes the evening."""
    candidate = watchlist.WatchedRepo(full_name="a/b", status="candidate")
    green = watchlist.WatchedRepo(full_name="c/d", status="green")
    assert not candidate.actionable
    assert green.actionable


def test_remove_reports_whether_it_did_anything():
    book = watchlist.Watchlist(repo=[watchlist.WatchedRepo(full_name="acme/widget")])
    assert book.remove("acme/widget") is True
    assert book.remove("acme/widget") is False


class TestSplitName:
    def test_plain(self):
        assert split_name("acme/widget") == ("acme", "widget")

    def test_https_url(self):
        assert split_name("https://github.com/acme/widget") == ("acme", "widget")

    def test_url_with_trailing_path(self):
        assert split_name("https://github.com/acme/widget/issues/42") == ("acme", "widget")

    def test_ssh_remote(self):
        assert split_name("git@github.com:acme/widget.git") == ("acme", "widget")

    def test_rejects_a_bare_name(self):
        import pytest

        with pytest.raises(ValueError, match="owner/repo"):
            split_name("widget")
