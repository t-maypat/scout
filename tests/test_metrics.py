"""Scoring tests. No network: every case is a hand-built API response, which is the
whole reason build_health is a pure function.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from scout import metrics

NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
WINDOW = {
    "lookback_days": 180,
    "contest_window_days": 90,
    "stale_assignment_days": 21,
    "abandoned_pr_days": 30,
    "free_hours": (20, 24),
}


def ts(days_ago: float = 0, hour: int | None = None) -> str:
    stamp = NOW - timedelta(days=days_ago)
    if hour is not None:
        stamp = stamp.replace(hour=hour, minute=0)
    return stamp.isoformat().replace("+00:00", "Z")


def merged_pr(assoc: str, days_ago: float = 10, open_days: float = 3) -> dict:
    return {
        "number": 1,
        "createdAt": ts(days_ago + open_days),
        "mergedAt": ts(days_ago),
        "authorAssociation": assoc,
        "additions": 10,
        "deletions": 2,
        "author": {"login": "someone"},
    }


def overview(merged: list[dict], **repo_kwargs) -> dict:
    repo = {
        "nameWithOwner": "acme/widget",
        "description": "a widget",
        "stargazerCount": 4200,
        "forkCount": 300,
        "isArchived": False,
        "isFork": False,
        "pushedAt": ts(1),
        "hasIssuesEnabled": True,
        "primaryLanguage": {"name": "Python"},
        "licenseInfo": {"spdxId": "MIT"},
        "defaultBranchRef": {"name": "main"},
        "openIssues": {"totalCount": 120},
        "openPRs": {"totalCount": 20},
        "merged": {"nodes": merged},
    }
    repo.update(repo_kwargs)
    return {"repository": repo}


def issue(
    assoc: str = "NONE",
    days_ago: float = 10,
    comments: list[dict] | None = None,
    labels: list[str] | None = None,
) -> dict:
    return {
        "number": 7,
        "title": "something is broken",
        "url": "https://github.com/acme/widget/issues/7",
        "createdAt": ts(days_ago),
        "closedAt": None,
        "authorAssociation": assoc,
        "author": {"login": "reporter"},
        "labels": {"nodes": [{"name": n} for n in (labels or [])]},
        "comments": {"nodes": comments or []},
    }


def comment(assoc: str, days_ago: float, hour: int | None = None) -> dict:
    return {"createdAt": ts(days_ago, hour), "authorAssociation": assoc, "author": {"login": "m"}}


def issues_payload(nodes: list[dict]) -> dict:
    return {"repository": {"issues": {"nodes": nodes}}}


def stale_payload(assigned: list[dict] | None = None, prs: list[dict] | None = None) -> dict:
    return {
        "repository": {
            "assigned": {"nodes": assigned or []},
            "openPRs": {"nodes": prs or []},
        }
    }


def build(overview_data, issues_data=None, stale_data=None) -> metrics.RepoHealth:
    return metrics.build_health(
        overview_data,
        issues_data or issues_payload([]),
        stale_data or stale_payload(),
        now=NOW,
        **WINDOW,
    )


class TestMergeOpenness:
    def test_closed_shop_is_a_trap(self):
        """Forty merges, every one from the team. The star count is irrelevant."""
        health = build(overview([merged_pr("MEMBER") for _ in range(40)]))
        label, reasons = metrics.verdict(health)
        assert label == metrics.TRAP
        assert health.cold_merges == 0
        assert any("first-time contributor" in r for r in reasons)

    def test_open_repo_is_good(self):
        merged = [merged_pr("MEMBER") for _ in range(20)]
        merged += [merged_pr("FIRST_TIME_CONTRIBUTOR") for _ in range(6)]
        merged += [merged_pr("CONTRIBUTOR") for _ in range(10)]
        health = build(overview(merged))
        assert health.cold_merges == 6
        assert health.outsider_merge_rate == pytest.approx(16 / 36)
        assert metrics.verdict(health)[0] == metrics.GOOD

    def test_contributor_counts_as_outsider_but_not_cold(self):
        health = build(overview([merged_pr("CONTRIBUTOR")]))
        assert health.outsider_merge_rate == 1.0
        assert health.cold_merges == 0

    def test_merges_outside_the_window_are_ignored(self):
        health = build(overview([merged_pr("FIRST_TIME_CONTRIBUTOR", days_ago=400)]))
        assert health.merged_sample == 0

    def test_too_few_merges_is_thin_not_a_verdict(self):
        health = build(overview([merged_pr("MEMBER") for _ in range(5)]))
        assert metrics.verdict(health)[0] == metrics.THIN

    def test_archived_short_circuits_everything(self):
        merged = [merged_pr("FIRST_TIME_CONTRIBUTOR") for _ in range(30)]
        health = build(overview(merged, isArchived=True))
        assert metrics.verdict(health) == (metrics.DEAD, ["archived"])


class TestResponsiveness:
    def test_median_reply_time_uses_first_maintainer_comment(self):
        nodes = [
            issue(
                "NONE",
                days_ago=10,
                comments=[comment("NONE", 9.9), comment("MEMBER", 9)],
            )
        ]
        health = build(overview([]), issues_payload(nodes))
        assert health.median_hours_to_maintainer_reply == pytest.approx(24, abs=0.1)
        assert health.unanswered_outsider_issues == 0

    def test_silence_is_counted_not_averaged_away(self):
        """Ignored issues must not vanish from the median - they are the signal."""
        nodes = [issue("NONE", days_ago=d) for d in (5, 6, 7)]
        nodes.append(issue("NONE", days_ago=8, comments=[comment("MEMBER", 7)]))
        health = build(overview([]), issues_payload(nodes))
        assert health.outsider_issues == 4
        assert health.unanswered_outsider_issues == 3

    def test_mostly_ignored_repo_is_a_trap(self):
        nodes = [issue("NONE", days_ago=i) for i in range(1, 13)]
        merged = [merged_pr("CONTRIBUTOR") for _ in range(25)]
        health = build(overview(merged), issues_payload(nodes))
        label, reasons = metrics.verdict(health)
        assert label == metrics.TRAP
        assert any("1 in 5" in r for r in reasons)


class TestContestedness:
    def test_beginner_issue_claimed_fast(self):
        nodes = [
            issue(
                "MEMBER",
                days_ago=10,
                labels=["good first issue"],
                comments=[comment("NONE", 10 - 20 / 1440)],
            )
        ]
        health = build(overview([]), issues_payload(nodes))
        assert health.beginner_issues_sampled == 1
        assert health.beginner_contest_minutes == pytest.approx(20, abs=1)

    def test_fast_claims_downgrade_a_healthy_repo_to_viable(self):
        merged = [merged_pr("FIRST_TIME_CONTRIBUTOR") for _ in range(15)]
        merged += [merged_pr("MEMBER") for _ in range(15)]
        nodes = [
            issue(
                "MEMBER",
                days_ago=d,
                labels=["good first issue"],
                comments=[comment("NONE", d - 10 / 1440)],
            )
            for d in (5, 10, 15)
        ]
        health = build(overview(merged), issues_payload(nodes))
        label, reasons = metrics.verdict(health)
        assert label == metrics.VIABLE
        assert any("do not race" in r for r in reasons)

    def test_maintainer_comment_is_not_a_claim(self):
        nodes = [
            issue(
                "MEMBER",
                days_ago=10,
                labels=["good first issue"],
                comments=[comment("MEMBER", 9.5)],
            )
        ]
        health = build(overview([]), issues_payload(nodes))
        assert health.beginner_contest_minutes is None


class TestClock:
    def test_us_pacific_maintainers_are_detected(self):
        """Activity peaking at 21:00 UTC is a 14:00 local day on the US west coast."""
        nodes = [
            issue("MEMBER", days_ago=d, comments=[comment("MEMBER", d - 0.1, hour=21)])
            for d in range(1, 15)
        ]
        health = build(overview([]), issues_payload(nodes))
        assert health.maintainer_utc_offset == pytest.approx(-7, abs=1)
        assert "Americas" in health.timezone_note

    def test_european_maintainers_land_in_office_hours(self):
        nodes = [
            issue("MEMBER", days_ago=d, comments=[comment("MEMBER", d - 0.1, hour=13)])
            for d in range(1, 15)
        ]
        health = build(overview([]), issues_payload(nodes))
        assert health.maintainer_utc_offset == pytest.approx(1, abs=1)
        # 13:00 UTC is 18:30 IST - before the 20:00 free window opens.
        assert health.free_hour_overlap == 0.0

    def test_evening_ist_overlap_is_counted(self):
        # 16:00 UTC is 21:30 IST, inside the 20-24 window.
        nodes = [
            issue("MEMBER", days_ago=d, comments=[comment("MEMBER", d - 0.1, hour=16)])
            for d in range(1, 15)
        ]
        health = build(overview([]), issues_payload(nodes))
        assert health.free_hour_overlap == 1.0

    def test_scattered_activity_reports_no_clock(self):
        nodes = [
            issue("MEMBER", days_ago=d, comments=[comment("MEMBER", d - 0.1, hour=(d * 2) % 24)])
            for d in range(1, 13)
        ]
        health = build(overview([]), issues_payload(nodes))
        assert health.maintainer_utc_offset is None
        assert "spread out" in health.timezone_note


class TestOpportunities:
    def assigned(self, idle_days: float, cross_refs: int = 0, assignees=("ghost",)) -> dict:
        return {
            "number": 42,
            "title": "refactor the thing",
            "url": "https://github.com/acme/widget/issues/42",
            "createdAt": ts(120),
            "updatedAt": ts(idle_days),
            "assignees": {"nodes": [{"login": a} for a in assignees]},
            "crossRefs": {"totalCount": cross_refs},
        }

    def open_pr(self, idle_days: float, assoc: str = "CONTRIBUTOR", draft: bool = False) -> dict:
        return {
            "number": 99,
            "title": "fix the thing",
            "url": "https://github.com/acme/widget/pull/99",
            "createdAt": ts(idle_days + 10),
            "updatedAt": ts(idle_days),
            "isDraft": draft,
            "authorAssociation": assoc,
            "author": {"login": "wanderer"},
        }

    def test_stale_assignment_is_found(self):
        health = build(overview([]), None, stale_payload(assigned=[self.assigned(40)]))
        assert [o.kind for o in health.opportunities] == ["stale-assignment"]
        assert health.opportunities[0].idle_days == 40
        assert "ghost" in health.opportunities[0].note

    def test_recent_assignment_is_left_alone(self):
        health = build(overview([]), None, stale_payload(assigned=[self.assigned(5)]))
        assert health.opportunities == []

    def test_linked_pr_suppresses_the_suggestion(self):
        """Work already in flight. Asking about it is the noise scout exists to avoid."""
        stale = stale_payload(assigned=[self.assigned(90, cross_refs=2)])
        health = build(overview([]), None, stale)
        assert health.opportunities == []

    def test_unassigned_stale_issue_is_not_a_stale_assignment(self):
        health = build(
            overview([]), None, stale_payload(assigned=[self.assigned(90, assignees=())])
        )
        assert health.opportunities == []

    def test_abandoned_pr_is_found(self):
        health = build(overview([]), None, stale_payload(prs=[self.open_pr(45)]))
        assert [o.kind for o in health.opportunities] == ["abandoned-pr"]

    def test_draft_and_maintainer_prs_are_skipped(self):
        stale = stale_payload(
            prs=[self.open_pr(45, draft=True), self.open_pr(45, assoc="MEMBER")]
        )
        health = build(overview([]), None, stale)
        assert health.opportunities == []

    def test_opportunities_are_ordered_by_how_long_they_have_sat(self):
        stale = stale_payload(assigned=[self.assigned(30)], prs=[self.open_pr(120)])
        health = build(overview([]), None, stale)
        assert [o.idle_days for o in health.opportunities] == [120, 30]


def test_ranking_puts_the_open_repo_first():
    closed = build(overview([merged_pr("MEMBER") for _ in range(40)]))
    merged = [merged_pr("FIRST_TIME_CONTRIBUTOR") for _ in range(10)]
    merged += [merged_pr("MEMBER") for _ in range(10)]
    open_repo = build(overview(merged))
    assert metrics.rank([closed, open_repo])[0] is open_repo


class TestCoverage:
    """The sample is a fixed count, not a time window. These prove the card says so."""

    def busy_repo(self, count: int, days_apart: float):
        """A repo whose page size runs out long before the lookback window does."""
        return [merged_pr("MEMBER", days_ago=1 + i * days_apart) for i in range(count)]

    def test_uncapped_sample_covers_the_whole_window(self):
        health = metrics.build_health(
            overview(self.busy_repo(30, 5)),
            issues_payload([]),
            stale_payload(),
            now=NOW,
            merged_requested=100,
            issues_requested=60,
            **WINDOW,
        )
        assert health.merged_coverage.returned == 30
        assert not health.merged_coverage.capped
        assert not health.merged_coverage.partial
        assert "whole 180d window" in health.merged_coverage.describe()

    def test_capped_short_sample_is_partial(self):
        """100 merges inside 9 days: the 180-day claim is fiction."""
        health = metrics.build_health(
            overview(self.busy_repo(100, 0.09)),
            issues_payload([]),
            stale_payload(),
            now=NOW,
            merged_requested=100,
            issues_requested=60,
            **WINDOW,
        )
        assert health.merged_coverage.capped
        assert health.merged_coverage.partial
        assert health.merged_coverage.span_days == pytest.approx(9, abs=0.5)
        assert "9d of 180d" in health.merged_coverage.describe()

    def test_capped_but_wide_sample_is_not_partial(self):
        health = metrics.build_health(
            overview(self.busy_repo(100, 1.5)),
            issues_payload([]),
            stale_payload(),
            now=NOW,
            merged_requested=100,
            issues_requested=60,
            **WINDOW,
        )
        assert health.merged_coverage.capped
        assert not health.merged_coverage.partial

    def test_partial_coverage_is_a_caveat_not_a_downgrade(self):
        """Five first-timers merged in nine days is stronger evidence than five in six
        months. Coverage qualifies the latency numbers; it must not touch the verdict."""
        merged = [merged_pr("FIRST_TIME_CONTRIBUTOR", days_ago=1 + i * 0.09) for i in range(50)]
        merged += [merged_pr("MEMBER", days_ago=1 + i * 0.09) for i in range(50)]
        health = metrics.build_health(
            overview(merged),
            issues_payload([]),
            stale_payload(),
            now=NOW,
            merged_requested=100,
            issues_requested=60,
            **WINDOW,
        )
        label, reasons = metrics.verdict(health)
        assert label == metrics.GOOD
        assert any("under-counted" in r for r in reasons)

    def test_unknown_page_size_never_claims_to_be_capped(self):
        health = build(overview([merged_pr("MEMBER") for _ in range(40)]))
        assert not health.merged_coverage.capped
        assert metrics.coverage_caveats(health) == []

    def test_empty_sample_says_so(self):
        health = build(overview([]))
        assert health.merged_coverage.describe() == "no sample"
        assert health.merged_coverage.fraction is None
