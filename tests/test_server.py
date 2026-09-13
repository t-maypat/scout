"""Dashboard API. The parts that matter are the guards, not the rendering."""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from scout import server


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SCOUT_EVENTS_DIR", str(tmp_path / "events"))
    monkeypatch.setenv("SCOUT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SCOUT_WATCHLIST_PATH", str(tmp_path / "watchlist.toml"))
    monkeypatch.setenv("SCOUT_GITHUB_TOKEN", "ghp_test")
    from scout.config import get_settings

    get_settings.cache_clear()
    yield TestClient(server.create_app())
    get_settings.cache_clear()


def test_an_empty_install_answers_rather_than_erroring(client):
    assert client.get("/api/inbox").json() == {"items": [], "dismissed": 0}
    assert client.get("/api/overview").json()["repos"] == []


def test_the_glossary_explains_every_metric_the_page_shows(client):
    glossary = client.get("/api/glossary").json()
    for key in ("newness", "outsider_rate", "reply", "overlap", "cold_start", "coverage"):
        assert glossary[key]["body"] and glossary[key]["how"]


def test_the_kill_switch_stops_the_buttons_too(client, monkeypatch):
    """A guard that only covers the cron job is not a guard."""
    monkeypatch.setenv("SCOUT_ENABLED", "false")
    from scout.config import get_settings

    get_settings.cache_clear()
    assert client.post("/api/poll").status_code == 403
    assert client.post("/api/probe", json={"repo": "acme/widget"}).status_code == 403


def test_only_one_job_runs_at_a_time(client):
    """Two probes racing would spend double the budget for no benefit, and a button is
    easy to click twice."""
    server._job_lock.acquire()
    try:
        assert client.post("/api/poll").status_code == 409
    finally:
        server._job_lock.release()


def test_the_lock_is_released_after_a_failure(client):
    def boom():
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        server._run_exclusive(boom)
    assert server._job_lock.acquire(blocking=False)
    server._job_lock.release()


def test_polling_with_nothing_green_explains_itself(client):
    body = client.post("/api/poll").json()
    assert body["polled"] == 0
    assert "green" in body["note"]


def test_marking_an_unknown_status_is_refused(client):
    assert client.post("/api/mark", json={"repo": "a/b", "status": "shipped"}).status_code == 400


def test_dismissing_hides_an_item_from_discord_too(client):
    """The dashboard and the digest read the same record, so dismissing here is not a
    second source of truth that the notifier can disagree with."""
    client.post("/api/intent", json={"key": "acme/widget#1:abandoned-pr", "action": "dismiss"})
    from scout.config import get_settings
    from scout.events import EventLog
    from scout.notify import already_sent

    assert "acme/widget#1:abandoned-pr" in already_sent(
        EventLog(get_settings().events_dir).read()
    )


def test_an_unknown_intent_is_refused(client):
    assert client.post("/api/intent", json={"key": "a/b#1:x", "action": "merge"}).status_code == 400


def test_concurrent_callers_do_not_both_get_the_lock():
    entered = []

    def slow():
        entered.append(1)
        threading.Event().wait(0.05)

    t = threading.Thread(target=lambda: server._run_exclusive(slow))
    t.start()
    threading.Event().wait(0.01)
    with pytest.raises(server.Busy):
        server._run_exclusive(slow)
    t.join()
    assert len(entered) == 1


class TestFailuresAreAnswers:
    """A 500 tells the person at the browser nothing. Every failure has a cause worth
    naming, and the page should show it."""

    def test_a_network_failure_is_a_502_not_a_500(self):
        import httpx

        err = server._as_http_error(httpx.ConnectError("no route"), "probing acme/widget")
        assert err.status_code == 502
        assert "Could not reach GitHub" in err.detail

    def test_a_slow_github_is_a_504(self):
        import httpx

        assert server._as_http_error(httpx.ReadTimeout("slow"), "probing").status_code == 504

    def test_the_whole_operation_deadline_is_a_504(self):
        from scout.github import Timeout

        err = server._as_http_error(Timeout("gave up after 90s"), "probing")
        assert err.status_code == 504
        assert "90s" in err.detail

    def test_rate_limiting_says_when_it_clears(self):
        from scout.github import RateLimited

        err = server._as_http_error(RateLimited(30), "probing")
        assert err.status_code == 429
        assert "hourly" in err.detail

    def test_a_missing_repo_does_not_leak_the_query(self):
        from scout.github import NotFound

        assert server._as_http_error(NotFound("/graphql"), "probing").status_code == 404

    def test_a_genuine_bug_still_names_itself(self):
        err = server._as_http_error(KeyError("repository"), "probing acme/widget")
        assert err.status_code == 500
        assert "KeyError" in err.detail

    def test_an_http_exception_passes_through_unchanged(self):
        original = server.Busy()
        assert server._as_http_error(original, "probing") is original


class TestEveryRouteLoads:
    """FastAPI builds a response model from each return annotation, so a bad one is a
    startup error rather than a 500 - and the suite has to catch it before the browser
    does."""

    def test_the_app_constructs(self):
        assert server.create_app() is not None

    def test_docs_are_served_rendered_from_the_tracked_file(self, client):
        r = client.get("/docs")
        assert r.status_code == 200
        assert "<h1>" in r.text, "markdown should be rendered, not dumped as text"
        assert "<table>" in r.text
        assert "docs/DOCUMENTATION" in r.text

    def test_both_documents_are_reachable(self, client):
        for page in ("DOCUMENTATION", "DECISIONS"):
            assert client.get(f"/docs/{page}").status_code == 200

    def test_links_between_the_documents_are_rewritten_to_routes(self, client):
        """DOCUMENTATION.md links to DECISIONS.md by filename, which is right on GitHub
        and a dead link once the same file is served as a route."""
        assert 'href="/docs/DECISIONS' in client.get("/docs").text

    def test_a_missing_document_explains_itself_rather_than_404ing_blankly(self, client):
        r = client.get("/docs/NOPE")
        assert r.status_code == 404
        assert "No documentation in this install" in r.text

    def test_a_traversal_attempt_cannot_escape_the_docs_directory(self, client):
        assert client.get("/docs/..%2f..%2fpyproject").status_code in (404, 400)

    def test_every_get_route_answers(self, client):
        for path in ("/", "/docs", "/api/glossary", "/api/overview", "/api/inbox"):
            assert client.get(path).status_code == 200, path

    def test_an_unknown_repo_detail_is_empty_not_an_error(self, client):
        body = client.get("/api/repo/acme/widget").json()
        assert body["entry"] is None and body["health"] is None
