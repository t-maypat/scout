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
    from scout.events import EventLog
    from scout.notify import already_sent
    from scout.config import get_settings

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
