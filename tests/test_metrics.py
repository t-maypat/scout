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


def merged_pr(
    assoc: str,
    days_ago: float = 10,
    open_days: float = 3,
    author: str = "someone",
    bot: bool = False,
    merged_by: str | None = None,
) -> dict:
    return {
        "mergedBy": ({"__typename": "User", "login": merged_by} if merged_by else None),
        "number": 1,
        "createdAt": ts(days_ago + open_days),
        "mergedAt": ts(days_ago),
        "authorAssociation": assoc,
        "additions": 10,
        "deletions": 2,
        "author": {"__typename": "Bot" if bot else "User", "login": author},
    }


def merge_history(
    *,
    days: int,
    per_day: int = 5,
    new_every: int = 0,
    assoc: str = "CONTRIBUTOR",
    bot_every: int = 0,
) -> list[dict]:
    """`per_day` merges a day across `days` days, oldest first.

    Every `new_every`-th merge comes from a login never seen before; the rest recycle a
    small pool of veterans. That is what the derived newness metric actually measures,
    so the fixtures have to have real author history rather than a single name.
    """
    nodes: list[dict] = []
    n = 0
    for day in range(days, 0, -1):
        for _ in range(per_day):
            n += 1
            if bot_every and n % bot_every == 0:
                nodes.append(merged_pr(assoc, days_ago=day, author="dependabot[bot]", bot=True))
                continue
            new = new_every and n % new_every == 0
            login = f"newcomer{n}" if new else f"veteran{n % 8}"
            nodes.append(merged_pr(assoc, days_ago=day, author=login))
    return nodes


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


def maintainer_chatter(count: int = 6) -> list[dict]:
    """Issues the maintainers did engage with.

    Fixtures need these or the reply metric is unmeasurable - which is now a real state,
    not an oversight: with no maintainer comment anywhere, scout cannot tell silence from
    an association it could not read, and refuses to convict on either.
    """
    return [
        issue("MEMBER", days_ago=10 + i, comments=[comment("MEMBER", 9 + i)])
        for i in range(count)
    ]


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
        """Six hundred merges across four months, not one of them a new name."""
        health = build(overview(merge_history(days=120, per_day=5, assoc="MEMBER")))
        label, reasons = metrics.verdict(health)
        assert label == metrics.TRAP
        assert health.newness.successes == 0
        assert any("closed shop" in r for r in reasons)

    def test_a_small_closed_sample_is_not_enough_to_convict(self):
        """The whole point of the interval: forty merges cannot settle this either way,
        and the old absolute-count rule convicted on exactly this evidence."""
        health = build(overview(merge_history(days=120, per_day=1)))
        assert health.newness.successes == 0
        assert health.newness.upper > metrics.CLOSED_SHOP_CEILING
        assert metrics.verdict(health)[0] != metrics.TRAP

    def test_open_repo_is_good(self):
        """One merge in five is somebody's first, across four months."""
        health = build(overview(merge_history(days=120, per_day=5, new_every=5)))
        assert health.newness_sufficient
        assert health.newness.lower > metrics.OPEN_DOOR_FLOOR
        assert metrics.verdict(health)[0] == metrics.GOOD

    def test_bots_are_kept_out_of_the_denominator(self):
        """Automation merges constantly and is never a newcomer, so leaving it in
        quietly deflates the rate."""
        health = build(overview(merge_history(days=120, per_day=5, new_every=5, bot_every=2)))
        assert health.bots_excluded == 300
        assert health.newness.total < 300

    def test_contributor_counts_as_outsider_but_not_cold(self):
        health = build(overview([merged_pr("CONTRIBUTOR")]))
        assert health.outsider_merge_rate.point == 1.0
        assert health.cold_merges == 0

    def test_merges_outside_the_window_are_ignored(self):
        health = build(overview([merged_pr("FIRST_TIME_CONTRIBUTOR", days_ago=400)]))
        assert health.merged_sample == 0

    def test_too_few_merges_is_thin_not_a_verdict(self):
        health = build(overview([merged_pr("MEMBER") for _ in range(5)]))
        assert metrics.verdict(health)[0] == metrics.THIN
        assert not health.newness_sufficient

    def test_archived_short_circuits_everything(self):
        merged = merge_history(days=120, per_day=5, new_every=5)
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
        # Too few merges to say anything about newcomers, so silence is the only
        # evidence there is - and it convicts.
        nodes = [issue("NONE", days_ago=20 + i) for i in range(20)] + maintainer_chatter()
        health = build(overview(merge_history(days=60, per_day=1)), issues_payload(nodes))
        label, reasons = metrics.verdict(health)
        assert label == metrics.TRAP
        assert any("go unanswered" in r for r in reasons)


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
        merged = merge_history(days=120, per_day=5, new_every=5)
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
    closed = build(overview(merge_history(days=120, per_day=5, assoc="MEMBER")))
    open_repo = build(overview(merge_history(days=120, per_day=5, new_every=5)))
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

    def test_coverage_is_reported_but_no_longer_gates_the_verdict(self):
        """Coverage was a proxy for 'sample too small to conclude'. The confidence
        interval measures that directly and continuously, so coverage is now only
        reported - it still qualifies the latency numbers, which have no interval."""
        health = metrics.build_health(
            overview(merge_history(days=4, per_day=40, new_every=5)),
            issues_payload([]),
            stale_payload(),
            now=NOW,
            merged_requested=100,
            issues_requested=60,
            **WINDOW,
        )
        assert health.merged_coverage.partial
        assert metrics.coverage_caveats(health)
        # The interval, not the coverage flag, is what refuses here.
        assert not health.newness_sufficient
        assert metrics.verdict(health)[0] == metrics.THIN

    def test_unknown_page_size_never_claims_to_be_capped(self):
        health = build(overview([merged_pr("MEMBER") for _ in range(40)]))
        assert not health.merged_coverage.capped
        assert metrics.coverage_caveats(health) == []

    def test_empty_sample_says_so(self):
        health = build(overview([]))
        assert health.merged_coverage.describe() == "no sample"
        assert health.merged_coverage.fraction is None


class TestFastRepoIsNotATrap:
    """Regression: BerriAI/litellm scored TRAP with an 80% outsider merge rate.

    A fixed hundred-row sample covered two days of a hundred-and-eighty-day window. In
    two days on a repo merging fifty PRs a day, everyone who lands is already a repeat
    CONTRIBUTOR - the first-timers from three months ago are outside the sample entirely.
    So `cold_merges == 0` was an artefact of the page size, not a fact about the project.

    The principle: a rate survives a short window, a count of a rare event does not.
    """

    def litellm_shaped(self, *, hours_old: float = 1.0):
        # 100 merges, all from established outside contributors, inside two days.
        merged = [merged_pr("CONTRIBUTOR", days_ago=i * 0.02, open_days=0.25) for i in range(80)]
        merged += [merged_pr("MEMBER", days_ago=i * 0.02) for i in range(20)]
        # 60 issues from outsiders, most of them only hours old, none answered yet.
        nodes = [
            issue("NONE", days_ago=hours_old / 24 + i * 0.05, comments=[]) for i in range(60)
        ]
        return metrics.build_health(
            overview(merged, stargazerCount=58605),
            issues_payload(nodes),
            stale_payload(),
            now=NOW,
            merged_requested=100,
            issues_requested=60,
            unanswered_after_hours=72.0,
            **WINDOW,
        )

    def test_it_is_not_a_trap(self):
        health = self.litellm_shaped()
        label, reasons = metrics.verdict(health)
        assert label != metrics.TRAP, reasons

    def test_it_refuses_to_judge_rather_than_guessing(self):
        label, reasons = metrics.verdict(self.litellm_shaped())
        assert label == metrics.THIN
        assert any("burn-in" in r for r in reasons)
        assert any("--pages" in r for r in reasons)

    def test_the_rate_is_still_reported_because_a_rate_survives_a_short_window(self):
        health = self.litellm_shaped()
        assert health.outsider_merge_rate.point == pytest.approx(0.8)
        assert any("80% of merges came from outside" in r for r in metrics.verdict(health)[1])

    def test_issues_younger_than_the_threshold_are_not_counted_as_ignored(self):
        """An issue opened forty minutes ago is not being neglected."""
        health = self.litellm_shaped(hours_old=1.0)
        assert health.unanswered_outsider_issues == 0
        assert health.outsider_issues == 0

    def test_genuinely_old_silence_still_counts(self):
        nodes = [issue("NONE", days_ago=20 + i) for i in range(24)]
        nodes += maintainer_chatter()
        health = metrics.build_health(
            overview(merge_history(days=60, per_day=1)),
            issues_payload(nodes),
            stale_payload(),
            now=NOW,
            merged_requested=0,
            issues_requested=0,
            unanswered_after_hours=72.0,
            **WINDOW,
        )
        assert health.unanswered_outsider_issues == 24
        assert metrics.verdict(health)[0] == metrics.TRAP

    def test_a_deeply_sampled_repo_with_no_newcomers_is_still_a_trap(self):
        """The refusal must not become a blanket excuse. Enough merges over enough
        months with nobody new is still damning, and the interval says so."""
        health = build(overview(merge_history(days=150, per_day=3)))
        assert health.newness_sufficient
        assert health.newness.upper < metrics.CLOSED_SHOP_CEILING
        assert metrics.verdict(health)[0] == metrics.TRAP


class TestNewnessIsDerivedNotBorrowed:
    """authorAssociation describes how someone is associated *now*, so a contributor who
    broke in six months ago reads as an established CONTRIBUTOR today and their first
    merge vanishes from any historical window. Deriving newness from author logins is
    immune to that, and comparing the two is the cheapest test of whether the field can
    be trusted about the past."""

    def test_newness_is_found_even_when_association_says_nobody_is_new(self):
        # Every row claims CONTRIBUTOR - exactly what a read-time association looks like.
        health = build(overview(merge_history(days=120, per_day=5, new_every=5)))
        assert health.cold_merges == 0
        assert health.newness.successes > 0
        assert health.association_disagrees

    def test_the_disagreement_is_reported_in_the_verdict(self):
        health = build(overview(merge_history(days=120, per_day=5, new_every=5)))
        assert any("describes today" in r for r in metrics.verdict(health)[1])

    def test_burn_in_stops_veterans_being_counted_as_new(self):
        """Everyone appears for the first time at some point in the sample. Without a
        burn-in the first sighting of a ten-year maintainer reads as a newcomer."""
        health = build(overview(merge_history(days=120, per_day=5, new_every=0)))
        assert health.newness.successes == 0

    def test_a_sample_too_short_for_a_burn_in_refuses(self):
        health = build(overview(merge_history(days=3, per_day=100, new_every=5)))
        assert not health.newness_sufficient
        assert "burn-in" in health.newness_note

    def test_a_sample_too_small_refuses_before_it_even_tries(self):
        health = build(overview(merge_history(days=60, per_day=1, new_every=5)))
        assert not health.newness_sufficient
        assert "baseline" in health.newness_note

    def test_bot_merges_never_count_as_newcomers(self):
        """Every bot login is new the first time it appears, and it is not a person."""
        health = build(overview(merge_history(days=120, per_day=4, bot_every=4)))
        assert health.bots_excluded > 0
        assert health.newness.successes == 0


class TestVerdictReadsTheInterval:
    def test_the_same_rate_at_two_sample_sizes_gives_different_verdicts(self):
        """8% of 25 and 8% of 600 are the same number and not the same evidence."""
        small = build(overview(merge_history(days=90, per_day=1, new_every=12)))
        large = build(overview(merge_history(days=150, per_day=8, new_every=12)))
        assert metrics.verdict(large)[0] == metrics.GOOD
        assert metrics.verdict(small)[0] != metrics.GOOD

    def test_a_wide_interval_refuses_rather_than_guessing(self):
        """21 of 64 is 33%, which looks excellent, but the interval runs 23% to 45%.
        Enough data to compute a number, not enough to stand behind one."""
        health = build(overview(merge_history(days=80, per_day=1, new_every=3)))
        assert health.newness_sufficient
        assert health.newness.undetermined
        assert metrics.verdict(health)[0] == metrics.THIN

    def test_ranking_breaks_ties_by_confidence_not_by_midpoint(self):
        """Identical point estimates, different sample sizes. The one that is actually
        settled has the higher lower bound, and must rank first."""
        small = build(overview(merge_history(days=90, per_day=1, new_every=12)))
        large = build(overview(merge_history(days=150, per_day=8, new_every=12)))
        assert small.newness.point == pytest.approx(large.newness.point, abs=0.001)
        assert large.newness.lower > small.newness.lower
        assert metrics.rank([small, large])[0] is large


class TestMergesOutweighSilence:
    """Found on BerriAI/litellm: 5.7% of merges were somebody's first, merged in a median
    of six hours, and it scored TRAP because 1152 of 1152 outsider issues went
    unanswered.

    Those measure different things. Issue silence says the maintainers are not talking;
    merged pull requests from newcomers say patches land anyway. Scout's question is
    whether a patch lands, so when the two disagree the merges decide it - and the
    silence becomes advice about how to approach the project.
    """

    def ignored_but_merging(self, new_every: int):
        nodes = [issue("NONE", days_ago=20 + i) for i in range(30)] + maintainer_chatter()
        return build(
            overview(merge_history(days=120, per_day=5, new_every=new_every)),
            issues_payload(nodes),
        )

    def test_a_repo_that_merges_newcomers_is_not_a_trap_for_ignoring_issues(self):
        health = self.ignored_but_merging(new_every=5)
        assert health.unanswered_rate.lower > metrics.IGNORED_FLOOR
        assert metrics.verdict(health)[0] != metrics.TRAP

    def test_the_silence_becomes_advice_instead(self):
        label, reasons = metrics.verdict(self.ignored_but_merging(new_every=5))
        assert label == metrics.VIABLE
        assert any("send code rather than questions" in r for r in reasons)

    def test_silence_still_convicts_when_newcomers_do_not_land(self):
        health = self.ignored_but_merging(new_every=0)
        label, reasons = metrics.verdict(health)
        assert label == metrics.TRAP
        assert any("go unanswered" in r for r in reasons)


class TestMissingCommentsAreVisible:
    """Zero maintainer comments across a thousand issues is far more likely to mean the
    comments were not fetched than that nobody ever replied. The card has to be able to
    say which, or an unreadable verdict looks like a fact about the project."""

    def test_an_empty_comment_sample_is_called_out(self):
        nodes = [issue("NONE", days_ago=20 + i, comments=[]) for i in range(30)]
        health = build(overview(merge_history(days=60, per_day=1)), issues_payload(nodes))
        assert health.maintainer_comments_seen == 0
        assert any("not fetched" in r for r in metrics.verdict(health)[1])

    def test_a_populated_comment_sample_says_nothing(self):
        nodes = [
            issue("NONE", days_ago=20 + i, comments=[comment("MEMBER", 19 + i)])
            for i in range(30)
        ]
        health = build(overview(merge_history(days=60, per_day=1)), issues_payload(nodes))
        assert health.maintainer_comments_seen == 30
        assert not any("not fetched" in r for r in metrics.verdict(health)[1])


class TestUnmeasurableIsNotBad:
    """BerriAI/litellm returned zero maintainer comments across 180 issues while merging
    outsider pull requests in four hours. Maintainers that active are commenting; the
    field simply was not identifying them - authorAssociation reports MEMBER only for
    publicly visible org membership. A rule fed by a field that returned nothing must not
    convict."""

    def ignored_with_no_readable_maintainer(self):
        nodes = [issue("NONE", days_ago=20 + i) for i in range(30)]
        return build(overview(merge_history(days=60, per_day=1)), issues_payload(nodes))

    def test_silence_does_not_convict_when_no_maintainer_is_identifiable(self):
        health = self.ignored_with_no_readable_maintainer()
        assert health.maintainer_comments_seen == 0
        assert not any("go unanswered" in r for r in metrics.verdict(health)[1])

    def test_comments_present_but_unattributable_says_so(self):
        """Comments exist, none recognised as a maintainer: the association is the
        problem, and the advice is different from a fetch failure."""
        nodes = [
            issue("NONE", days_ago=20 + i, comments=[comment("NONE", 19 + i)])
            for i in range(30)
        ]
        health = build(overview(merge_history(days=60, per_day=1)), issues_payload(nodes))
        assert health.comments_seen == 30
        assert health.maintainer_comments_seen == 0
        assert any("private org membership" in r for r in metrics.verdict(health)[1])

    def test_no_comments_at_all_is_reported_as_a_fetch_problem(self):
        health = self.ignored_with_no_readable_maintainer()
        assert health.comments_seen == 0
        assert any("not fetched" in r for r in metrics.verdict(health)[1])


class TestMaintainersComeFromMerges:
    """BerriAI has zero public organisation members, so authorAssociation returns NONE or
    CONTRIBUTOR for everybody on litellm - 100 recent comments, not one MEMBER, OWNER or
    COLLABORATOR. The field cannot name a maintainer there at all.

    Merging is a permission. Whoever did it had write access, and the probe already
    fetches who it was.
    """

    def test_the_merger_is_recognised_as_a_maintainer(self):
        nodes = [merged_pr("NONE", author="stranger", merged_by="ishaan")]
        assert metrics.maintainers_from_merges(nodes) == frozenset({"ishaan"})

    def test_bots_that_merge_are_not_maintainers(self):
        nodes = [
            {"mergedBy": {"__typename": "Bot", "login": "mergify"}},
            {"mergedBy": {"__typename": "User", "login": "renovate[bot]"}},
        ]
        assert metrics.maintainers_from_merges(nodes) == frozenset()

    def test_an_unmerged_or_unknown_merger_contributes_nothing(self):
        assert metrics.maintainers_from_merges([{"mergedBy": None}, {}]) == frozenset()

    def test_a_reply_from_a_merger_counts_even_when_the_association_hides_it(self):
        """The whole point: litellm's maintainers reply as NONE."""
        merged = [merged_pr("NONE", days_ago=d, author="x", merged_by="ishaan")
                  for d in range(1, 40)]
        nodes = [
            issue("NONE", days_ago=20 + i,
                  comments=[{"createdAt": ts(19 + i), "authorAssociation": "NONE",
                             "author": {"login": "ishaan"}}])
            for i in range(12)
        ]
        health = build(overview(merged), issues_payload(nodes))
        assert health.maintainers == ["ishaan"]
        assert health.maintainer_comments_seen == 12
        assert health.unanswered_outsider_issues == 0

    def test_without_the_merge_set_those_replies_would_be_invisible(self):
        """Same data, no mergedBy: every reply vanishes and the repo looks ignored."""
        merged = [merged_pr("NONE", days_ago=d, author="x") for d in range(1, 40)]
        nodes = [
            issue("NONE", days_ago=20 + i,
                  comments=[{"createdAt": ts(19 + i), "authorAssociation": "NONE",
                             "author": {"login": "ishaan"}}])
            for i in range(12)
        ]
        health = build(overview(merged), issues_payload(nodes))
        assert health.maintainer_comments_seen == 0
        assert health.unanswered_outsider_issues == 12

    def test_a_maintainers_own_stale_pr_is_not_offered_to_you(self):
        merged = [merged_pr("NONE", days_ago=d, author="x", merged_by="ishaan")
                  for d in range(1, 40)]
        stale = stale_payload(prs=[{
            "number": 99, "title": "wip", "url": "u",
            "createdAt": ts(120), "updatedAt": ts(60), "isDraft": False,
            "authorAssociation": "NONE", "author": {"login": "ishaan"},
        }])
        health = build(overview(merged), issues_payload([]), stale)
        assert health.opportunities == []
