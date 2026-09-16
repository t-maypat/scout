"""Fresh and free: is there something here I could pick up tonight?

The other rules answer "has this been abandoned", which only needs the listing. This one
answers "is anyone already on it", which the listing cannot say at all - so enrichment
feeds in linked pull requests and comment authors, and these tests supply those as events
exactly as `scout enrich` writes them. No network anywhere.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from scout import derive, enrich, notify
from scout.events import ISSUE_ENRICHED, Event
from scout.poll import to_event

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
REPO = "acme/widget"


def iso(days_ago: float = 0) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z")


def issue_row(
    number: int = 7,
    *,
    labels: tuple[str, ...] = (),
    assignees: tuple[str, ...] = (),
    author: str = "reporter",
    association: str = "NONE",
    comments: int = 0,
    created: float = 2,
    updated: float = 1,
    state: str = "open",
) -> dict:
    return {
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


def comment(body: str, *, author: str = "passerby", association: str = "NONE") -> dict:
    return {
        "author": author,
        "association": association,
        "created_at": iso(0.5),
        "body": body,
    }


def enrichment(
    number: int = 7,
    *,
    linked: tuple[dict, ...] = (),
    comments: tuple[dict, ...] = (),
    updated: float = 1,
) -> Event:
    """What `scout enrich` writes, for one version of one issue."""
    return Event.make(
        kind=ISSUE_ENRICHED,
        repo=REPO,
        subject=number,
        occurred_at=iso(updated),
        payload={"number": number, "linked_prs": list(linked), "comments": list(comments)},
        observed_at=NOW,
    )


def fresh_items(*events: Event, **kw) -> list[derive.Opportunity]:
    """The fresh-and-free opportunities derived from these events."""
    state = derive.derive(events)
    found = derive.opportunities(state, now=NOW, **kw)
    return [o for o in found if o.kind == "fresh-and-free"]


def observed(row: dict) -> Event:
    return to_event(row, REPO, observed_at=NOW)


class TestWhatCountsAsFreshAndFree:
    def test_a_triaged_unclaimed_issue_is_offered(self):
        found = fresh_items(observed(issue_row(labels=("confirmed",))), enrichment())
        assert [o.number for o in found] == [7]
        assert "labelled confirmed" in found[0].note

    def test_a_maintainer_reply_is_validation_enough(self):
        """The label is one signal; a maintainer engaging at all is the other. Either
        means somebody who can merge has looked at it."""
        found = fresh_items(
            observed(issue_row()),
            enrichment(comments=(comment("good catch, worth fixing", association="MEMBER"),)),
        )
        assert [o.note for o in found] == ["a maintainer replied, no PR, nobody claiming it"]

    def test_an_untriaged_issue_is_not_offered(self):
        """Nobody who can merge has said this is real work. That is `unanswered-report`
        territory - reproducing it - not something to go and build."""
        assert fresh_items(observed(issue_row()), enrichment()) == []

    def test_an_issue_with_an_open_pull_request_is_not_offered(self):
        found = fresh_items(
            observed(issue_row(labels=("confirmed",))),
            enrichment(linked=({"number": 91, "state": "OPEN", "draft": False},)),
        )
        assert found == []

    def test_a_closed_pull_request_does_not_block_it(self):
        """Somebody tried and gave up. That is the opening, not the obstacle."""
        found = fresh_items(
            observed(issue_row(labels=("confirmed",))),
            enrichment(linked=({"number": 91, "state": "CLOSED", "draft": False},)),
        )
        assert [o.number for o in found] == [7]

    def test_somebody_asking_to_take_it_counts_as_claimed(self):
        found = fresh_items(
            observed(issue_row(labels=("confirmed",))),
            enrichment(comments=(comment("Can I work on this one?"),)),
        )
        assert found == []

    def test_a_maintainer_inviting_work_is_not_a_claim(self):
        """`PRs welcome, I will take a look when it lands` is the opposite of a claim, and
        it is written by the person whose words would otherwise read like one."""
        found = fresh_items(
            observed(issue_row(labels=("confirmed",))),
            enrichment(
                comments=(
                    comment(
                        "PRs welcome, I will take a look when it lands",
                        author="owner",
                        association="MEMBER",
                    ),
                )
            ),
        )
        assert [o.number for o in found] == [7]

    def test_an_unenriched_issue_is_never_offered(self):
        """The refusal that matters: with no answer about links and claims, "free" is
        unknown, not true. Offering it would send you into a race you cannot see."""
        assert fresh_items(observed(issue_row(labels=("confirmed",)))) == []

    def test_enrichment_for_an_older_version_does_not_count(self):
        """The issue moved after the answer was fetched, so whatever changed - a new
        comment, a linked PR - is exactly what is not known."""
        found = fresh_items(
            observed(issue_row(labels=("confirmed",), updated=1)), enrichment(updated=3)
        )
        assert found == []

    def test_an_old_issue_falls_out_of_the_window(self):
        found = fresh_items(
            observed(issue_row(labels=("confirmed",), created=30, updated=1)), enrichment()
        )
        assert found == []

    def test_a_question_is_not_work(self):
        found = fresh_items(
            observed(issue_row(labels=("confirmed", "needs-info"))), enrichment()
        )
        assert found == []

    def test_beginner_labelled_issues_stay_out_of_it(self):
        """Unchanged rule, restated here because this is the one that would quietly
        reintroduce the race for `good first issue`."""
        found = fresh_items(
            observed(issue_row(labels=("confirmed", "good first issue"))), enrichment()
        )
        assert found == []

    def test_an_assigned_issue_is_not_free(self):
        found = fresh_items(
            observed(issue_row(labels=("confirmed",), assignees=("someone",))), enrichment()
        )
        assert found == []

    def test_the_newest_enrichment_wins(self):
        """A claim that arrived in a later fetch has to beat the earlier clean answer."""
        row = issue_row(labels=("confirmed",), updated=1)
        found = fresh_items(
            observed(row),
            enrichment(updated=1),
            enrichment(updated=1, comments=(comment("I am working on this"),)),
        )
        assert found == []


class TestTheLabelSetsDoNotContradict:
    def test_no_label_both_invites_work_and_starts_a_race(self):
        """`help wanted` was in both: counted as a maintainer inviting work, and excluded
        as beginner real estate bots race. One of those had to win, and the exclusion
        does - so the accepting set must never grow back into it."""
        from scout.metrics import BEGINNER_LABELS

        assert not (derive.ACCEPTING_LABELS & BEGINNER_LABELS)

    def test_bug_is_not_treated_as_triage(self):
        """Issue templates apply it on filing, so it says what the reporter clicked."""
        assert "bug" not in derive.ACCEPTING_LABELS


class TestRanking:
    def test_fresh_work_outranks_what_has_been_rotting(self):
        """An abandoned pull request will still be there next week. A fresh issue will
        not, so it goes at the top of the digest."""
        state = derive.derive(
            [
                observed(issue_row(7, labels=("confirmed",), created=1, updated=1)),
                enrichment(7),
                observed(
                    {
                        **issue_row(9, created=120, updated=60),
                        "pull_request": {"merged_at": None},
                        "draft": False,
                    }
                ),
            ]
        )
        found = derive.opportunities(state, now=NOW)
        assert [o.kind for o in found][0] == "fresh-and-free"
        assert "abandoned-pr" in [o.kind for o in found]


class TestItGoesInItsOwnThread:
    CH = "chan"

    class FakeDiscord:
        def __init__(self):
            self.calls = []
            self.next_id = 100

        def __call__(self, method, path, payload=None):
            self.calls.append((method, path, payload))
            self.next_id += 1
            return {"id": str(self.next_id)}

    def item(self, kind: str, number: int) -> notify.Item:
        return notify.Item.from_opportunity(
            derive.Opportunity(
                REPO, number, kind, f"thing {number}",
                f"https://github.com/{REPO}/issues/{number}", 1, "note",
            )
        )

    def day(self):
        from datetime import date

        return date(2026, 9, 17)

    def headers(self, fake):
        path = f"/channels/{self.CH}/messages"
        return [c for c in fake.calls if c[0] == "POST" and c[1] == path]

    def test_fresh_items_get_their_own_thread_beside_the_usual_one(self, tmp_path):
        fake = self.FakeDiscord()
        digest = notify.Digest(
            items=[self.item("fresh-and-free", 1), self.item("abandoned-pr", 2)]
        )
        notify.send_threaded(digest, self.CH, fake, tmp_path / "t.json", self.day())
        assert [h[2]["content"] for h in self.headers(fake)] == [
            "17 Sep - acme/widget - fresh (1)",
            "17 Sep - acme/widget (1)",
        ]
        assert len([c for c in fake.calls if c[1].endswith("/threads")]) == 2

    def test_each_thread_keeps_its_own_count(self, tmp_path):
        state = tmp_path / "t.json"
        import json

        notify.send_threaded(
            notify.Digest(items=[self.item("fresh-and-free", 1)]),
            self.CH, self.FakeDiscord(), state, self.day(),
        )
        notify.send_threaded(
            notify.Digest(items=[self.item("abandoned-pr", 2)]),
            self.CH, self.FakeDiscord(), state, self.day(),
        )
        saved = json.loads(state.read_text(encoding="utf-8"))["2026-09-17"]
        assert sorted(saved) == [f"{REPO}:fresh", f"{REPO}:work"]

    def test_the_header_carries_a_check_now_button_the_worker_understands(self, tmp_path):
        fake = self.FakeDiscord()
        notify.send_threaded(
            notify.Digest(items=[self.item("fresh-and-free", 1)]),
            self.CH, fake, tmp_path / "t.json", self.day(),
        )
        row = self.headers(fake)[0][2]["components"][0]
        custom_id = row["components"][0]["custom_id"]
        # The same shape worker/src/index.js matches with CONTROL_ID.
        assert re.match(r"^(poll):([\w.-]+/[\w.-]+)$", custom_id), custom_id
        assert custom_id == f"poll:{REPO}"


class TestEnrichmentAsksAboutCandidatesOnly:
    def state_with(self, *events: Event) -> derive.State:
        return derive.derive(events)

    def test_a_current_answer_is_not_asked_for_again(self):
        """The expensive half of this is the request, so an issue whose current version
        already has an answer is skipped entirely."""
        state = self.state_with(observed(issue_row()), enrichment())
        assert enrich.candidates(state, now=NOW) == []

    def test_an_issue_that_moved_since_its_answer_is_asked_again(self):
        state = self.state_with(observed(issue_row(updated=1)), enrichment(updated=3))
        assert [s.number for s in enrich.candidates(state, now=NOW)] == [7]

    def test_assigned_and_old_issues_are_not_candidates(self):
        state = self.state_with(
            observed(issue_row(1, assignees=("someone",))),
            observed(issue_row(2, created=90, updated=80)),
            observed(issue_row(3)),
        )
        assert [s.number for s in enrich.candidates(state, now=NOW)] == [3]

    def test_the_youngest_candidates_win_a_short_budget(self):
        """If the budget runs out it should run out on the issues least likely to still
        be free tomorrow."""
        state = self.state_with(
            observed(issue_row(1, created=6, updated=6)),
            observed(issue_row(2, created=1, updated=1)),
            observed(issue_row(3, created=3, updated=3)),
        )
        assert [s.number for s in enrich.candidates(state, now=NOW, limit=2)] == [2, 3]

    def test_one_query_covers_a_batch_of_issues(self):
        document = enrich.document([7, 9])
        assert "i7: issue(number: 7)" in document
        assert "i9: issue(number: 9)" in document
        assert "mutation" not in document, "the read-only guard rejects those, rightly"

    def test_the_answer_is_identified_by_the_version_it_describes(self):
        """Same issue, same version, asked twice: one row in the log."""
        node = {"number": 7, "timelineItems": {"nodes": []}, "comments": {"nodes": []}}
        updated = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
        first = enrich.to_event(REPO, node, updated, NOW)
        second = enrich.to_event(REPO, node, updated, NOW + timedelta(hours=3))
        assert first.id == second.id

    def test_linked_pull_requests_are_read_from_both_timeline_shapes(self):
        node = {
            "number": 7,
            "timelineItems": {
                "nodes": [
                    {"source": {"number": 91, "state": "OPEN", "isDraft": False}},
                    {"subject": {"number": 92, "state": "CLOSED", "isDraft": False}},
                    {"source": {}},
                ]
            },
            "comments": {"nodes": [{"author": {"login": "x"}, "body": "hi",
                                    "authorAssociation": "NONE", "createdAt": iso()}]},
        }
        event = enrich.to_event(REPO, node, None, NOW)
        assert [pr["number"] for pr in event.payload["linked_prs"]] == [91, 92]
        assert event.payload["comments"][0]["author"] == "x"

    def test_a_long_comment_is_truncated_rather_than_logged_whole(self):
        node = {
            "number": 7,
            "timelineItems": {"nodes": []},
            "comments": {
                "nodes": [
                    {
                        "author": {"login": "x"},
                        "body": "y" * 5000,
                        "authorAssociation": "NONE",
                        "createdAt": iso(),
                    }
                ]
            },
        }
        event = enrich.to_event(REPO, node, None, NOW)
        assert len(event.payload["comments"][0]["body"]) == enrich.BODY_CHARS
