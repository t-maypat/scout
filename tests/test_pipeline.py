"""Poller, event log, derivation and digest. No network anywhere."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from scout import derive, notify
from scout.cursors import Cursor
from scout.events import ISSUE_OBSERVED, PR_OBSERVED, Event, EventLog
from scout.github import ConditionalResponse
from scout.poll import poll_repo, to_event

NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
REPO = "acme/widget"


def iso(days_ago: float = 0) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z")


def row(
    number: int = 42,
    *,
    pull: bool = False,
    state: str = "open",
    labels: tuple[str, ...] = (),
    assignees: tuple[str, ...] = (),
    author: str = "reporter",
    association: str = "NONE",
    comments: int = 0,
    created: float = 30,
    updated: float = 1,
    draft: bool = False,
    merged: bool = False,
) -> dict:
    item = {
        "number": number,
        "title": f"issue {number}",
        "html_url": f"https://github.com/{REPO}/issues/{number}",
        "state": state,
        "labels": [{"name": n} for n in labels],
        "assignees": [{"login": a} for a in assignees],
        "user": {"login": author},
        "author_association": association,
        "comments": comments,
        "created_at": iso(created),
        "updated_at": iso(updated),
    }
    if pull:
        item["pull_request"] = {"merged_at": iso(updated) if merged else None}
        item["draft"] = draft
    return item


class FakeClient:
    """Stands in for GitHubClient. Records the conditional headers it was given."""

    def __init__(self, pages: list[list[dict]] | None = None, unchanged: bool = False):
        self.pages = pages or []
        self.unchanged = unchanged
        self.calls: list[tuple[str, str | None]] = []
        self.rest_requests = 0
        self.rest_not_modified = 0
        self.rest_remaining = 4999

    def rest_conditional(self, path: str, etag: str | None = None) -> ConditionalResponse:
        self.calls.append((path, etag))
        self.rest_requests += 1
        if self.unchanged:
            self.rest_not_modified += 1
            return ConditionalResponse(status=304, body=None, etag=etag)
        index = len(self.calls) - 1
        body = self.pages[index] if index < len(self.pages) else []
        return ConditionalResponse(status=200, body=body, etag=f"etag-{index}")


class TestEventIdentity:
    def test_same_observation_produces_the_same_id(self):
        a = to_event(row(), REPO, observed_at=NOW)
        b = to_event(row(), REPO, observed_at=NOW + timedelta(hours=3))
        assert a.id == b.id, "identity is what was seen, not when we looked"

    def test_a_changed_observation_is_a_different_event(self):
        a = to_event(row(updated=1), REPO)
        b = to_event(row(updated=0.5), REPO)
        assert a.id != b.id

    def test_pull_requests_are_recognised_by_their_marker(self):
        assert to_event(row(pull=True), REPO).kind == PR_OBSERVED
        assert to_event(row(), REPO).kind == ISSUE_OBSERVED

    def test_merge_is_carried_on_the_payload(self):
        event = to_event(row(pull=True, state="closed", merged=True), REPO)
        assert event.payload["merged"] is True

    def test_round_trip_through_json(self):
        event = to_event(row(labels=("bug",), assignees=("ghost",)), REPO)
        assert Event.from_json(event.to_json()) == event


class TestEventLog:
    def test_second_run_writes_nothing(self, tmp_path):
        """The property that makes an overlapping cron schedule safe."""
        log = EventLog(tmp_path)
        events = [to_event(row(n), REPO) for n in range(5)]
        assert len(log.append(events)) == 5
        assert log.append(events) == []
        assert log.count() == 5

    def test_duplicates_inside_one_batch_collapse(self, tmp_path):
        log = EventLog(tmp_path)
        assert len(log.append([to_event(row(), REPO), to_event(row(), REPO)])) == 1

    def test_log_survives_a_reopen(self, tmp_path):
        EventLog(tmp_path).append([to_event(row(), REPO)])
        assert EventLog(tmp_path).count() == 1

    def test_sharded_by_month_of_observation(self, tmp_path):
        log = EventLog(tmp_path)
        log.append([to_event(row(1), REPO, observed_at=NOW)])
        log.append([to_event(row(2), REPO, observed_at=NOW - timedelta(days=60))])
        assert {p.name for p in log.shards()} == {"2026-09.jsonl", "2026-07.jsonl"}

    def test_reading_filters_by_repo_and_kind(self, tmp_path):
        log = EventLog(tmp_path)
        log.append([to_event(row(1), REPO), to_event(row(2, pull=True), "other/repo")])
        assert len(list(log.read(repo=REPO))) == 1
        assert len(list(log.read(kinds=[PR_OBSERVED]))) == 1


class TestPolling:
    def test_a_304_costs_nothing_and_writes_nothing(self):
        client = FakeClient(unchanged=True)
        result, cursor = poll_repo(client, REPO, Cursor(etag="abc"), now=NOW)
        assert result.unchanged and result.events == []
        assert cursor.quiet_polls == 1
        assert cursor.etag == "abc", "an unchanged poll must not discard the etag"

    def test_the_etag_is_sent_on_the_first_page_only(self):
        client = FakeClient(pages=[[row(n, updated=1) for n in range(100)], [row(200)]])
        poll_repo(client, REPO, Cursor(etag="abc"), per_page=100, now=NOW)
        assert client.calls[0][1] == "abc"
        assert client.calls[1][1] is None, "page two has a different url, so a different etag"

    def test_the_url_carries_no_since_parameter(self):
        """A moving `since=` would invalidate the etag on every poll."""
        client = FakeClient(pages=[[row()]])
        poll_repo(client, REPO, Cursor(), now=NOW)
        assert "since=" not in client.calls[0][0]
        assert "sort=updated&direction=desc" in client.calls[0][0]

    def test_walking_stops_at_the_watermark(self):
        pages = [[row(1, updated=0.5), row(2, updated=2), row(3, updated=5)]]
        client = FakeClient(pages=pages)
        watermark = NOW - timedelta(days=1)
        result, _ = poll_repo(client, REPO, Cursor(watermark=watermark), now=NOW)
        assert [e.subject for e in result.events] == ["1"]

    def test_the_watermark_advances_to_the_newest_row_seen(self):
        client = FakeClient(pages=[[row(1, updated=0.5), row(2, updated=3)]])
        _, cursor = poll_repo(client, REPO, Cursor(), now=NOW)
        assert cursor.watermark == datetime.fromisoformat(iso(0.5).replace("Z", "+00:00"))

    def test_a_short_page_ends_the_walk(self):
        """One request for the changes, then one for the stalest page."""
        client = FakeClient(pages=[[row(1)], [row(2)]])
        poll_repo(client, REPO, Cursor(), per_page=100, now=NOW)
        assert len(client.calls) == 2
        assert "direction=desc" in client.calls[0][0]
        assert "direction=asc" in client.calls[1][0]


class TestDerivation:
    def log_of(self, *rows: dict) -> list[Event]:
        return [to_event(r, REPO, observed_at=NOW) for r in rows]

    def test_latest_observation_wins(self):
        state = derive.derive(
            self.log_of(row(updated=5, state="open"), row(updated=1, state="closed"))
        )
        assert state.subjects[f"{REPO}#42"].state == "closed"

    def test_an_older_snapshot_never_overwrites_a_newer_one(self):
        """Shards can be read out of order; the newest observation has to survive it."""
        events = self.log_of(row(updated=1, state="closed"))
        events += self.log_of(row(updated=9, state="open"))
        state = derive.derive(events)
        assert state.subjects[f"{REPO}#42"].state == "closed"

    def test_unassignment_is_detected(self):
        state = derive.derive(
            self.log_of(row(updated=5, assignees=("ghost",)), row(updated=1))
        )
        moves = [t for t in state.transitions if t.what == "unassigned"]
        assert len(moves) == 1
        assert moves[0].detail == "ghost"

    def test_labels_and_closure_are_detected(self):
        state = derive.derive(
            self.log_of(row(updated=5), row(updated=1, state="closed", labels=("bug",)))
        )
        assert {t.what for t in state.transitions} == {"labeled", "closed"}

    def test_a_merged_pr_reports_merged_not_closed(self):
        state = derive.derive(
            self.log_of(
                row(pull=True, updated=5),
                row(pull=True, updated=1, state="closed", merged=True),
            )
        )
        assert [t.what for t in state.transitions] == ["merged"]

    def test_a_malformed_row_does_not_poison_the_replay(self):
        bad = Event.make(ISSUE_OBSERVED, REPO, 1, iso(1), {"no": "number"})
        state = derive.derive([bad, *self.log_of(row(7))])
        assert f"{REPO}#7" in state.subjects

    def test_derivation_is_deterministic(self):
        events = self.log_of(row(1, updated=3), row(1, updated=1), row(2, pull=True))
        first, second = derive.derive(events), derive.derive(list(reversed(events)))
        assert first.subjects == second.subjects


class TestOpportunities:
    def state_of(self, *rows: dict) -> derive.State:
        return derive.derive([to_event(r, REPO, observed_at=NOW) for r in rows])

    def test_stale_assignment(self):
        found = derive.opportunities(
            self.state_of(row(assignees=("ghost",), updated=40)), now=NOW
        )
        assert [o.kind for o in found] == ["stale-assignment"]

    def test_a_fresh_assignment_is_left_alone(self):
        found = derive.opportunities(
            self.state_of(row(assignees=("ghost",), updated=3)), now=NOW
        )
        assert found == []

    def test_abandoned_pr(self):
        found = derive.opportunities(
            self.state_of(row(pull=True, association="CONTRIBUTOR", updated=45)), now=NOW
        )
        assert [o.kind for o in found] == ["abandoned-pr"]

    def test_a_maintainers_own_stale_pr_is_not_yours_to_take(self):
        found = derive.opportunities(
            self.state_of(row(pull=True, association="MEMBER", updated=45)), now=NOW
        )
        assert found == []

    def test_unanswered_outsider_report(self):
        found = derive.opportunities(
            self.state_of(row(created=3, updated=3, comments=0)), now=NOW
        )
        assert [o.kind for o in found] == ["unanswered-report"]

    def test_a_report_with_a_reply_is_not_an_opportunity(self):
        found = derive.opportunities(
            self.state_of(row(created=3, updated=3, comments=2)), now=NOW
        )
        assert found == []

    def test_beginner_labelled_issues_are_excluded_on_purpose(self):
        """The contested ones. Racing bots to them from IST is unwinnable."""
        found = derive.opportunities(
            self.state_of(row(created=3, updated=3, labels=("good first issue",))), now=NOW
        )
        assert found == []

    def test_a_closed_subject_is_never_an_opportunity(self):
        found = derive.opportunities(
            self.state_of(row(state="closed", assignees=("ghost",), updated=40)), now=NOW
        )
        assert found == []

    def test_repo_filter_applies(self):
        found = derive.opportunities(
            self.state_of(row(assignees=("ghost",), updated=40)), now=NOW, repos=["other/x"]
        )
        assert found == []


class TestDigest:
    def opportunity(self, number: int = 1, kind: str = "stale-assignment"):
        return derive.Opportunity(
            repo=REPO,
            number=number,
            kind=kind,
            title=f"thing {number}",
            url=f"https://github.com/{REPO}/issues/{number}",
            idle_days=40,
            note="assigned to ghost",
        )

    def test_already_sent_items_are_not_repeated(self):
        first = notify.build_digest([self.opportunity()])
        sent = notify.already_sent([notify.sent_event(first)])
        second = notify.build_digest([self.opportunity()], sent=sent)
        assert second.empty and second.skipped == 1

    def test_transitions_come_before_standing_opportunities(self):
        """Something that just came free beats something that has sat for forty days."""
        move = derive.Transition(REPO, 9, "issue", "unassigned", NOW, "ghost")
        built = notify.build_digest([self.opportunity()], [move])
        assert built.items[0].number == 9

    def test_the_digest_respects_its_size_limit(self):
        built = notify.build_digest([self.opportunity(n) for n in range(20)], max_items=3)
        assert len(built.items) == 3

    def test_embeds_stay_inside_discord_limits(self):
        built = notify.build_digest([self.opportunity(n) for n in range(20)], max_items=20)
        payload = built.to_discord()
        assert len(payload["embeds"]) <= notify.MAX_EMBEDS
        assert all(len(e["title"]) <= notify.MAX_TITLE for e in payload["embeds"])

    def test_a_very_long_title_is_clipped_not_dropped(self):
        long = derive.Opportunity(REPO, 1, "stale-assignment", "x" * 900, "u", 3, "n")
        payload = notify.build_digest([long]).to_discord()
        assert len(payload["embeds"][1]["title"]) <= notify.MAX_TITLE

    def test_an_empty_digest_reports_itself_empty(self):
        assert notify.build_digest([]).empty

    def test_posting_with_nowhere_to_send_is_an_error_not_a_silent_no_op(self):
        with pytest.raises(ValueError, match="nowhere to send"):
            notify.post({"content": "hi"})


class TestButtonsNeedTheBot:
    """Discord ignores interactive components from a webhook that is not owned by an
    application, so a webhook created by hand in the UI can never carry buttons however
    it is called. That is the only reason the bot transport exists."""

    def digest_of(self, n=3):
        return notify.build_digest([
            derive.Opportunity(REPO, i, "abandoned-pr", f"thing {i}",
                               f"https://github.com/{REPO}/pull/{i}", 40, "author gone")
            for i in range(n)
        ])

    def test_webhook_mode_sends_one_message_and_no_components(self):
        messages = self.digest_of().to_messages(with_buttons=False)
        assert len(messages) == 1
        assert "components" not in messages[0]

    def test_bot_mode_sends_a_header_then_one_message_per_item(self):
        """Components attach to a message, not an embed, so eight embeds in one message
        could only ever share one row of buttons."""
        messages = self.digest_of(3).to_messages(with_buttons=True)
        assert len(messages) == 4
        assert "components" not in messages[0], "the header carries no actions"
        assert all("components" in m for m in messages[1:])

    def test_the_link_button_needs_nothing_listening(self):
        row = self.digest_of(1).to_messages(with_buttons=True)[1]["components"][0]
        link = row["components"][0]
        assert link["style"] == notify.LINK and link["url"].startswith("https://")
        assert "custom_id" not in link, "a link button is handled by Discord itself"

    def test_action_ids_match_what_the_receiver_parses(self):
        import re

        pattern = re.compile(r"^(claim|dispatch|snooze|dismiss):([\w.-]+/[\w.-]+):(\d+)$")
        row = self.digest_of(1).to_messages(with_buttons=True)[1]["components"][0]
        ids = [c["custom_id"] for c in row["components"] if "custom_id" in c]
        assert ids and all(pattern.match(i) for i in ids), ids

    def test_a_digest_never_exceeds_the_embed_ceiling(self):
        messages = self.digest_of(30).to_messages(with_buttons=True)
        assert len(messages) <= notify.MAX_EMBEDS
        assert all(len(m["embeds"]) == 1 for m in messages)


class TestPollSideUsesTheSameMaintainerSet:
    """The poll has the same blind spot and cannot fix it alone: the issues endpoint
    never says who merged anything. The probe works it out and records it; derivation
    reads it from the watchlist."""

    def state_with(self, **kw):
        return derive.derive([to_event(row(**kw), REPO, observed_at=NOW)])

    def test_a_maintainers_stale_pr_is_not_an_opportunity(self):
        state = self.state_with(
            number=99, pull=True, author="ishaan", association="NONE", updated=60, created=90
        )
        assert derive.opportunities(state, now=NOW) != []
        assert derive.opportunities(
            state, now=NOW, maintainers={REPO: ["ishaan"]}
        ) == []

    def test_a_maintainers_own_issue_is_not_an_unanswered_report(self):
        state = self.state_with(
            number=7, author="ishaan", association="NONE", comments=0, updated=3, created=3
        )
        assert derive.opportunities(state, now=NOW) != []
        assert derive.opportunities(state, now=NOW, maintainers={REPO: ["ishaan"]}) == []

    def test_an_outsider_is_still_an_outsider(self):
        state = self.state_with(
            number=8, author="stranger", association="NONE", comments=0, updated=3, created=3
        )
        found = derive.opportunities(state, now=NOW, maintainers={REPO: ["ishaan"]})
        assert [o.kind for o in found] == ["unanswered-report"]

    def test_the_set_is_scoped_to_its_own_repo(self):
        state = self.state_with(
            number=99, pull=True, author="ishaan", association="NONE", updated=60, created=90
        )
        found = derive.opportunities(state, now=NOW, maintainers={"other/repo": ["ishaan"]})
        assert [o.kind for o in found] == ["abandoned-pr"]


class TestTheStalestPage:
    """Sorting newest-first and walking toward a watermark can never reach the oldest
    rows, and the oldest rows are the entire point - an abandoned pull request is one
    nothing has touched for ninety days. On litellm those sit thousands of rows past any
    page budget, so the inbox would never have shown a single one of them."""

    def test_the_far_end_is_fetched_and_scoped_to_open_items(self):
        client = FakeClient(pages=[[], [row(9, pull=True, updated=95, created=150)]])
        result, cursor = poll_repo(client, REPO, Cursor(), now=NOW)
        assert "direction=asc" in client.calls[1][0]
        assert "state=open" in client.calls[1][0], "a stale closed item is not work"
        assert result.stale_seen == 1
        assert [e.subject for e in result.events] == ["9"]

    def test_it_carries_its_own_etag(self):
        """Different url, different ETag - and this one 304s almost every time, because
        by definition nothing at that end is moving."""
        client = FakeClient(pages=[[], [row(9)]])
        _, cursor = poll_repo(client, REPO, Cursor(), now=NOW)
        assert cursor.stale_etag and cursor.stale_etag != cursor.etag
        poll_repo(client, REPO, cursor, now=NOW)
        assert client.calls[-1][1] == cursor.stale_etag

    def test_re_observing_the_same_stale_rows_writes_nothing(self, tmp_path):
        """It is a standing view, not a stream, so it returns the same rows every time."""
        client = FakeClient(pages=[[], [row(9, updated=95)]])
        log = EventLog(tmp_path)
        first, cursor = poll_repo(client, REPO, Cursor(), now=NOW)
        assert len(log.append(first.events)) == 1
        again, _ = poll_repo(client, REPO, Cursor(), now=NOW + timedelta(hours=2))
        assert log.append(again.events) == []

    def test_a_failure_at_the_far_end_does_not_lose_the_main_walk(self):
        class HalfBroken(FakeClient):
            def rest_conditional(self, path, etag=None):
                if "direction=asc" in path:
                    from scout.github import GitHubError

                    raise GitHubError("boom")
                return super().rest_conditional(path, etag)

        client = HalfBroken(pages=[[row(1)]])
        result, _ = poll_repo(client, REPO, Cursor(), now=NOW)
        assert result.ok and [e.subject for e in result.events] == ["1"]

    def test_an_abandoned_pr_from_the_far_end_reaches_the_inbox(self):
        """End to end: the far-end row becomes an observation, derivation turns it into
        an opportunity, and that is what the digest reads."""
        client = FakeClient(pages=[[], [row(9, pull=True, association="CONTRIBUTOR",
                                          author="wanderer", updated=95, created=150)]])
        result, _ = poll_repo(client, REPO, Cursor(), now=NOW)
        state = derive.derive(result.events)
        found = derive.opportunities(state, now=NOW)
        assert [o.kind for o in found] == ["abandoned-pr"]


class TestThreadPerRepoPerDay:
    """Per-item buttons force per-item messages, which is a wall in the channel. So the
    channel gets one line per repository per day, and each item goes in its thread."""

    CH = "chan"

    class FakeDiscord:
        def __init__(self):
            self.calls = []
            self.next_id = 100

        def __call__(self, method, path, payload=None):
            self.calls.append((method, path, payload))
            self.next_id += 1
            return {"id": str(self.next_id)}

    def item(self, repo, number):
        return notify.Item(
            f"{repo}#{number}:abandoned-pr", "abandoned-pr", repo, number,
            f"thing {number}", f"https://github.com/{repo}/pull/{number}", "author gone", 40,
        )

    def day(self, d=14):
        from datetime import date

        return date(2026, 9, d)

    def channel_posts(self, fake):
        path = f"/channels/{self.CH}/messages"
        return [c for c in fake.calls if c[0] == "POST" and c[1] == path]

    def thread_creates(self, fake):
        return [c for c in fake.calls if c[1].endswith("/threads")]

    def test_the_channel_gets_one_line_per_repo_and_items_go_in_threads(self, tmp_path):
        fake = self.FakeDiscord()
        digest = notify.Digest(
            items=[self.item("a/x", 1), self.item("b/y", 2), self.item("a/x", 3)]
        )
        sent = notify.send_threaded(digest, self.CH, fake, tmp_path / "t.json", self.day())
        assert [h[2]["content"] for h in self.channel_posts(fake)] == [
            "14 Sep - a/x (2)",
            "14 Sep - b/y (1)",
        ]
        assert len(self.thread_creates(fake)) == 2
        in_threads = [
            c for c in fake.calls
            if c[0] == "POST"
            and c[1] != f"/channels/{self.CH}/messages"
            and not c[1].endswith("/threads")
        ]
        assert len(in_threads) == 3
        assert all("components" in c[2] for c in in_threads), "each item keeps its buttons"
        assert sent == 5

    def test_repos_are_ordered_by_their_best_item(self, tmp_path):
        fake = self.FakeDiscord()
        digest = notify.Digest(items=[self.item("b/y", 1), self.item("a/x", 2)])
        notify.send_threaded(digest, self.CH, fake, tmp_path / "t.json", self.day())
        assert self.channel_posts(fake)[0][2]["content"] == "14 Sep - b/y (1)"

    def test_a_second_send_the_same_day_appends_instead_of_opening_another(self, tmp_path):
        import json

        state = tmp_path / "t.json"
        notify.send_threaded(
            notify.Digest(items=[self.item("a/x", 1)]), self.CH, self.FakeDiscord(),
            state, self.day(),
        )
        thread = json.loads(state.read_text(encoding="utf-8"))["2026-09-14"]["a/x:work"]["thread"]

        second = self.FakeDiscord()
        notify.send_threaded(
            notify.Digest(items=[self.item("a/x", 2), self.item("a/x", 3)]),
            self.CH, second, state, self.day(),
        )
        assert self.channel_posts(second) == [], "no new header line"
        assert self.thread_creates(second) == [], "no new thread"
        assert len([c for c in second.calls if c[1] == f"/channels/{thread}/messages"]) == 2
        patches = [c for c in second.calls if c[0] == "PATCH"]
        assert patches and patches[0][2]["content"] == "14 Sep - a/x (3)"

    def test_the_next_day_opens_a_new_thread(self, tmp_path):
        state = tmp_path / "t.json"
        notify.send_threaded(
            notify.Digest(items=[self.item("a/x", 1)]), self.CH, self.FakeDiscord(),
            state, self.day(14),
        )
        fake = self.FakeDiscord()
        notify.send_threaded(
            notify.Digest(items=[self.item("a/x", 2)]), self.CH, fake, state, self.day(15)
        )
        assert [h[2]["content"] for h in self.channel_posts(fake)] == ["15 Sep - a/x (1)"]

    def test_old_days_are_pruned_from_state(self, tmp_path):
        import json

        state = tmp_path / "t.json"
        for d in (1, 14):
            notify.send_threaded(
                notify.Digest(items=[self.item("a/x", d)]), self.CH, self.FakeDiscord(),
                state, self.day(d),
            )
        assert list(json.loads(state.read_text(encoding="utf-8"))) == ["2026-09-14"]

    def test_webhook_mode_still_sends_one_message(self, monkeypatch):
        posted = []
        monkeypatch.setattr(notify, "post", lambda payload, url="", *a, **k: posted.append(url))
        digest = notify.Digest(items=[self.item("a/x", 1), self.item("b/y", 2)])
        assert notify.send(digest, webhook_url="https://hook") == 1
        assert posted == ["https://hook"]

    def limited_after(self, fake, items):
        """A Discord that rate limits once `items` item messages have gone in."""

        def call(method, path, payload=None):
            # Item messages, not the thread header - that carries a button row of its own.
            if len([c for c in fake.calls if "embeds" in (c[2] or {})]) == items:
                raise RuntimeError("discord rate limited, retry after 0.3s")
            return fake(method, path, payload)

        return call

    def test_a_send_that_stops_partway_names_what_went_out(self, tmp_path):
        """The seventh of eight items hit a rate limit. Six were in Discord and nothing
        said so, so the next digest would have sent all six again."""
        import json

        state = tmp_path / "t.json"
        digest = notify.Digest(items=[self.item("a/x", n) for n in (1, 2, 3)])
        with pytest.raises(notify.SendFailed, match="rate limited") as failed:
            notify.send_threaded(
                digest, self.CH, self.limited_after(self.FakeDiscord(), 2), state, self.day()
            )
        assert failed.value.delivered == ["a/x#1:abandoned-pr", "a/x#2:abandoned-pr"]
        entry = json.loads(state.read_text(encoding="utf-8"))["2026-09-14"]["a/x:work"]
        assert entry["thread"] and entry["count"] == 2

    def test_finishing_a_partial_send_reuses_its_thread_and_fixes_the_count(self, tmp_path):
        state = tmp_path / "t.json"
        with pytest.raises(notify.SendFailed):
            notify.send_threaded(
                notify.Digest(items=[self.item("a/x", n) for n in (1, 2, 3)]),
                self.CH, self.limited_after(self.FakeDiscord(), 2), state, self.day(),
            )
        retry = self.FakeDiscord()
        notify.send_threaded(
            notify.Digest(items=[self.item("a/x", 3)]), self.CH, retry, state, self.day()
        )
        assert self.channel_posts(retry) == [] and self.thread_creates(retry) == []
        patches = [c for c in retry.calls if c[0] == "PATCH"]
        assert patches and patches[0][2]["content"] == "14 Sep - a/x (3)"

    def test_only_what_was_delivered_is_recorded_as_sent(self):
        digest = notify.Digest(items=[self.item("a/x", n) for n in (1, 2, 3)])
        went = digest.only(["a/x#1:abandoned-pr"])
        assert went.generated_at == digest.generated_at
        assert notify.already_sent([notify.sent_event(went)]) == {"a/x#1:abandoned-pr"}


class TestDiscordPacing:
    """Discord publishes no per-route limits, so pacing reads what responses say - and
    every wait it takes is capped, because the number is chosen by the other side."""

    class Clock:
        def __init__(self):
            self.now = 0.0
            self.slept = []

        def __call__(self):
            return self.now

        def sleep(self, seconds):
            self.slept.append(seconds)
            self.now += seconds

    def pacing(self, clock, **kw):
        return notify.Pacing(sleep=clock.sleep, clock=clock, **kw)

    @staticmethod
    def response(status=200, body=None, headers=None):
        import httpx

        return httpx.Response(
            status,
            json=body if body is not None else {},
            headers=headers or {},
            request=httpx.Request("POST", f"{notify.API}/channels/t/messages"),
        )

    def empty_bucket(self, reset_after):
        return self.response(headers={
            "X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": str(reset_after),
        })

    def test_a_short_rate_limit_is_waited_out_and_retried(self):
        clock = self.Clock()
        replies = iter([self.response(429, {"retry_after": 0.3}), self.response(200)])
        got = self.pacing(clock).request("r", lambda: next(replies))
        assert got.status_code == 200
        assert clock.slept == [0.3]

    def test_a_long_rate_limit_fails_at_once_rather_than_sleeping_the_cap(self):
        """Five seconds against a sixty-second limit only earns another 429, and 429s
        count toward Discord's invalid-request ban."""
        clock = self.Clock()
        with pytest.raises(RuntimeError, match="retry after 60s"):
            self.pacing(clock).request("r", lambda: self.response(429, {"retry_after": 60}))
        assert clock.slept == []

    def test_a_rate_limit_with_no_usable_wait_is_not_guessed(self):
        clock = self.Clock()
        with pytest.raises(RuntimeError, match=r"retry after \?s"):
            self.pacing(clock).request("r", lambda: self.response(429, {}))
        assert clock.slept == []

    def test_retries_are_limited(self):
        clock = self.Clock()
        calls = []

        def always_limited():
            calls.append(1)
            return self.response(429, {"retry_after": 0.5})

        with pytest.raises(RuntimeError, match="rate limited"):
            self.pacing(clock).request("r", always_limited)
        assert len(calls) == notify.RATE_LIMIT_RETRIES + 1

    def test_consecutive_requests_are_spaced(self):
        clock = self.Clock()
        pacing = self.pacing(clock)
        for _ in range(3):
            pacing.request("r", lambda: self.response())
        assert clock.slept == [notify.SEND_INTERVAL] * 2

    def test_an_empty_bucket_is_waited_out_before_that_route_is_asked_again(self):
        clock = self.Clock()
        pacing = self.pacing(clock, interval=0)
        pacing.request("POST /channels/t/messages", lambda: self.empty_bucket(2.5))
        pacing.request("POST /channels/other/messages", lambda: self.response())
        assert clock.slept == [], "another channel is another bucket"
        pacing.request("POST /channels/t/messages", lambda: self.response())
        assert clock.slept == [2.5]

    def test_a_bucket_wait_is_capped(self):
        clock = self.Clock()
        pacing = self.pacing(clock, interval=0)
        pacing.request("r", lambda: self.empty_bucket(3600))
        pacing.request("r", lambda: self.response())
        assert clock.slept == [notify.MAX_RATE_LIMIT_WAIT]

    def test_no_wait_runs_past_the_deadline(self):
        """The job is killed at ten minutes and the step after the send has to commit
        what went out, so the send gives up rather than being killed mid-sleep."""
        clock = self.Clock()
        pacing = self.pacing(clock, deadline=10)
        clock.now = 9.5
        with pytest.raises(RuntimeError, match="deadline"):
            pacing.request("r", lambda: self.response(429, {"retry_after": 1}))

    def test_the_bot_transport_retries_through_the_pacing(self, monkeypatch):
        clock = self.Clock()
        replies = iter([self.response(429, {"retry_after": 0.3}), self.response(200, {"id": "9"})])
        monkeypatch.setattr(notify.httpx, "request", lambda *a, **k: next(replies))
        call = notify.bot_request("token", pacing=self.pacing(clock))
        assert call("POST", "/channels/t/messages", {}) == {"id": "9"}
        assert clock.slept == [0.3]
