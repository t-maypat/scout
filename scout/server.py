"""The local dashboard.

The CLI is good at one repository at a time. This is for the other two jobs: deciding
what to spend an evening on, and comparing repositories against each other. It reads the
same event log and watchlist, and it never becomes a second source of truth.

Anything that costs rate limit - a probe, a poll - runs behind a single-flight lock and
the same safety guards the CLI uses. A button that spends budget needs to be as careful
as a cron job, arguably more so, because it is easy to click twice.
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from scout import cursors, derive, metrics, notify, watchlist
from scout.config import get_settings
from scout.events import PROBE_COMPLETED, Event, EventLog
from scout.github import GitHubClient, GitHubError, NotFound, RateLimited, Timeout
from scout.poll import poll_all
from scout.probe import probe as run_probe
from scout.safety import SafetyError, assert_enabled, assert_repo_cap

log = logging.getLogger("scout.server")

STATIC = Path(__file__).parent / "static"

# One job at a time. Two probes racing would spend double the budget and interleave
# writes to the same watchlist file for no benefit.
_job_lock = threading.Lock()


def _log() -> EventLog:
    return EventLog(get_settings().events_dir)


class Busy(HTTPException):
    def __init__(self) -> None:
        super().__init__(status_code=409, detail="Another job is already running.")


def _run_exclusive(work):
    if not _job_lock.acquire(blocking=False):
        raise Busy()
    try:
        return work()
    finally:
        _job_lock.release()


def _as_http_error(exc: Exception, what: str) -> HTTPException:
    """Turn anything a network call can raise into an answer the page can show.

    A 500 tells the person at the browser nothing and leaves them guessing whether they
    broke it. Every failure here has a cause worth naming - the token, the network,
    GitHub being down - so name it. The traceback still goes to the server log.
    """
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, NotFound):
        return HTTPException(404, "No such repository, or it is private.")
    if isinstance(exc, Timeout):
        return HTTPException(504, str(exc))
    if isinstance(exc, RateLimited):
        return HTTPException(429, "GitHub is rate limiting this token. It resets hourly.")
    if isinstance(exc, GitHubError | SafetyError | ValueError):
        return HTTPException(400, str(exc))
    if isinstance(exc, httpx.TimeoutException):
        return HTTPException(504, f"GitHub did not answer in time while {what}.")
    if isinstance(exc, httpx.HTTPError):
        return HTTPException(502, f"Could not reach GitHub while {what}: {exc}")
    # Anything left is a bug in scout, not a bad request. Log it in full and say so.
    log.exception("unhandled error while %s", what)
    return HTTPException(500, f"scout hit a bug while {what}: {type(exc).__name__}: {exc}")


# Plain-English explanations, served to the page so every number can say what it means.
# Written for someone who has never read the source, because that is who needs them.
GLOSSARY: dict[str, dict[str, str]] = {
    "newness": {
        "title": "Somebody's first merge",
        "body": (
            "Of the pull requests this project merged, how many were the author's first "
            "one here. This is the question that decides whether it is worth your time: "
            "a project can have a busy issue tracker and still never merge a patch from "
            "someone it does not already know."
        ),
        "how": (
            "Worked out from who wrote each merged pull request and when, not from "
            "GitHub's own label. GitHub tells you how someone is associated with a "
            "project today, so anyone who broke in months ago now looks like a regular "
            "and their first merge disappears."
        ),
    },
    "interval": {
        "title": "The range, not the number",
        "body": (
            "Every rate is shown as a range because a rate on its own hides how much "
            "was measured. One newcomer in twelve merges and twenty in two hundred and "
            "forty are both 8%, and only one of them settles anything."
        ),
        "how": (
            "A wide range means scout looked but cannot tell yet. Probe deeper and it "
            "narrows. It will not give you a confident answer it has not earned."
        ),
    },
    "outsider_rate": {
        "title": "Merges from outside the team",
        "body": (
            "The share of merged pull requests written by someone who is not a member "
            "or collaborator. High is good, but it counts people who have contributed "
            "before, so it does not tell you whether a stranger can get in."
        ),
        "how": "Survives a short sample well, because it is a rate over many merges.",
    },
    "reply": {
        "title": "Reply time to outsiders",
        "body": (
            "How long an issue opened by someone outside the team waits for a "
            "maintainer to say anything at all. Silence is counted only after three "
            "days, so a busy tracker full of fresh issues does not look neglectful."
        ),
        "how": "Measured on issues from outsiders only. Team chatter is ignored.",
    },
    "contest": {
        "title": "How fast beginner issues get claimed",
        "body": (
            "The time between a 'good first issue' appearing and the first outsider "
            "commenting on it. Minutes means you are racing bots and will lose from "
            "a different timezone."
        ),
        "how": "Scout deliberately does not surface these. It looks for quieter work.",
    },
    "overlap": {
        "title": "Active while you are free",
        "body": (
            "The share of maintainer activity that happens during the hours you set as "
            "free. Near zero means every question you ask costs a full day before "
            "anyone answers."
        ),
        "how": "Measured from comment timestamps, converted to your timezone.",
    },
    "coverage": {
        "title": "How much scout actually saw",
        "body": (
            "Metrics are advertised over six months, but each request returns a fixed "
            "number of rows. On a fast project those rows run out in days, so this says "
            "how far back the sample really reached."
        ),
        "how": "Raise the page budget to see further back.",
    },
    "cold_start": {
        "title": "Cold start",
        "body": (
            "Minutes from a fresh clone to a passing test suite, measured once by you. "
            "This is what decides whether an alert is worth acting on: a project you "
            "cannot build in ten minutes is one you will never touch on a weeknight."
        ),
        "how": "Recorded by hand when you mark a repo green.",
    },
    "verdict": {
        "title": "Verdicts",
        "body": (
            "Dead: archived or abandoned. Trap: active and popular, but it does not "
            "merge outsiders or answer them. Thin: not enough evidence to say either "
            "way. Viable: nothing disqualifying. Good: newcomers reliably get merged."
        ),
        "how": "Thin is a real answer, not a failure. It means look harder.",
    },
}


# Request bodies live at module scope on purpose. `from __future__ import annotations`
# turns every hint into a string, and FastAPI resolves a model's strings against module
# globals - so a model defined inside create_app() cannot be resolved and every request
# to it comes back 422.
class RepoRequest(BaseModel):
    repo: str
    why: str = ""


class MarkRequest(BaseModel):
    repo: str
    status: str
    setup: str = ""
    test: str = ""
    minutes: int | None = None
    notes: str = ""
    poll: bool | None = None


class IntentRequest(BaseModel):
    key: str
    action: str


# The docs live beside the package in a checkout and nowhere at all in a wheel, so look
# in both rather than assuming one. A missing file is a readable page, not a 404 body.
DOC_ROOTS = (
    Path(__file__).resolve().parent.parent / "docs",
    Path.cwd() / "docs",
)


def find_doc(name: str) -> Path | None:
    safe = Path(name).name.removesuffix(".md")
    for root in DOC_ROOTS:
        candidate = root / f"{safe}.md"
        if candidate.is_file():
            return candidate
    return None


def render_markdown(source: Path) -> str:
    from markdown_it import MarkdownIt

    text = source.read_text(encoding="utf-8")
    rendered = MarkdownIt("commonmark", {"html": False}).enable("table").render(text)
    # Relative links between the docs have to keep working once they are served as routes.
    return rendered.replace('href="DECISIONS.md', 'href="/docs/DECISIONS').replace(
        'href="DOCUMENTATION.md', 'href="/docs/DOCUMENTATION'
    )


DOCS_SHELL = """<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>scout - {title}</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&amp;family=IBM+Plex+Sans:wght@400;450;600&amp;display=swap">
<style>
 :root{{--ground:#e9edef;--surface:#fcfdfd;--ink:#1b2733;--muted:#607281;--rule:#c6d1d8;
   --rule-soft:#dde5e9;--accent:#37788a}}
 *{{box-sizing:border-box}}
 body{{margin:0;background:var(--ground);color:var(--ink);
   font:400 15px/1.62 "IBM Plex Sans",system-ui,sans-serif}}
 header{{background:var(--surface);border-bottom:1px solid var(--rule);
   padding:1rem 1.5rem;display:flex;gap:1.5rem;align-items:baseline;
   position:sticky;top:0}}
 header a{{color:var(--muted);text-decoration:none;border-bottom:1px solid transparent}}
 header a:hover{{color:var(--ink);border-bottom-color:var(--ink)}}
 header .home{{font-weight:600;color:var(--ink)}}
 article{{max-width:48rem;margin:0 auto;padding:2.5rem 1.5rem 6rem}}
 h1{{font-size:1.83rem;letter-spacing:-0.02em;margin:0 0 1.5rem}}
 h2{{font-size:1.37rem;margin:2.75rem 0 0.75rem;padding-top:1.25rem;
   border-top:1px solid var(--rule)}}
 h3{{font-size:1.1rem;margin:1.75rem 0 0.5rem}}
 p,li{{max-width:42rem}}
 a{{color:var(--accent)}}
 code{{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:0.88em;
   background:var(--surface);border:1px solid var(--rule-soft);border-radius:2px;
   padding:0.08em 0.3em}}
 pre{{background:var(--surface);border:1px solid var(--rule-soft);border-radius:3px;
   padding:0.9rem 1.1rem;overflow-x:auto}}
 pre code{{background:none;border:0;padding:0;font-size:0.84rem;line-height:1.55}}
 table{{border-collapse:collapse;width:100%;margin:1.25rem 0;font-size:0.9375rem;
   display:block;overflow-x:auto}}
 th,td{{text-align:left;padding:0.5rem 0.85rem 0.5rem 0;
   border-bottom:1px solid var(--rule-soft);vertical-align:top}}
 th{{font-weight:600;border-bottom-color:var(--rule)}}
 blockquote{{margin:1.25rem 0;padding:0.5rem 0 0.5rem 1rem;
   border-left:2px solid var(--accent);color:var(--muted)}}
 hr{{border:0;border-top:1px solid var(--rule);margin:2.5rem 0}}
 /* A markdown rule already separates the section; the heading must not draw a second. */
 hr + h2{{border-top:0;padding-top:0;margin-top:0}}
 @media (max-width:640px){{article{{padding:1.75rem 1.1rem 4rem}}}}
</style>
<header>
  <a class="home" href="/">scout</a>
  <a href="/docs/DOCUMENTATION">Documentation</a>
  <a href="/docs/DECISIONS">Design decisions</a>
</header>
<article>{body}</article>
"""


def _proportion(p: Any) -> dict[str, Any]:
    return {
        "point": p.point,
        "lower": p.lower,
        "upper": p.upper,
        "successes": p.successes,
        "total": p.total,
        "undetermined": p.undetermined,
    }


def _health_json(health: metrics.RepoHealth) -> dict[str, Any]:
    label, reasons = metrics.verdict(health)
    return {
        "full_name": health.full_name,
        "description": health.description,
        "stars": health.stars,
        "language": health.language,
        "days_since_push": health.days_since_push,
        "open_issues": health.open_issues,
        "open_prs": health.open_prs,
        "verdict": label,
        "reasons": reasons,
        "newness": _proportion(health.newness),
        "newness_sufficient": health.newness_sufficient,
        "newness_note": health.newness_note,
        "newness_scored_days": health.newness_scored_days,
        "bots_excluded": health.bots_excluded,
        "outsider_rate": _proportion(health.outsider_merge_rate),
        "merged_sample": health.merged_sample,
        "cold_merges": health.cold_merges,
        "association_disagrees": health.association_disagrees,
        "median_days_to_merge": health.median_days_to_merge_outsider,
        "median_hours_to_reply": health.median_hours_to_maintainer_reply,
        "unanswered": _proportion(health.unanswered_rate),
        "maintainer_comments_seen": health.maintainer_comments_seen,
        "comments_seen": health.comments_seen,
        "contest_minutes": health.beginner_contest_minutes,
        "overlap": health.free_hour_overlap,
        "timezone_note": health.timezone_note,
        "merged_coverage": health.merged_coverage.describe(),
        "issue_coverage": health.issue_coverage.describe(),
        "coverage_partial": health.merged_coverage.partial,
    }


def create_app() -> FastAPI:
    app = FastAPI(title="scout", docs_url=None, redoc_url=None)

    @app.get("/")
    def index() -> FileResponse:
        # The whole dashboard is one file, so a cached copy survives an upgrade and shows
        # yesterday's interface against today's API. Revalidate every load; it is a local
        # server serving one small file.
        return FileResponse(
            STATIC / "index.html",
            headers={"Cache-Control": "no-cache, must-revalidate"},
        )

    @app.get("/docs")
    @app.get("/docs/{page}")
    def docs(page: str = "DOCUMENTATION") -> HTMLResponse:
        """Serve the markdown docs, rendered, from whichever copy is actually present."""
        source = find_doc(page)
        if source is None:
            return HTMLResponse(
                DOCS_SHELL.format(
                    title="Documentation not found",
                    body=(
                        "<h1>No documentation in this install</h1><p>scout looked for "
                        f"<code>docs/{page}.md</code> next to the package and in the "
                        "working directory. Run the dashboard from a checkout, or read "
                        "the docs on GitHub.</p>"
                    ),
                ),
                status_code=404,
            )
        return HTMLResponse(
            DOCS_SHELL.format(title=source.stem.title(), body=render_markdown(source))
        )

    @app.get("/api/glossary")
    def glossary() -> dict[str, Any]:
        return GLOSSARY

    @app.get("/api/overview")
    def overview() -> dict[str, Any]:
        settings = get_settings()
        book = watchlist.load()
        marks = cursors.load()
        log = _log()
        state = derive.derive(log.read())

        polled = [e.full_name for e in book.repo if e.should_poll(settings.poll_only_actionable)]
        last = [c.last_polled for c in marks.repo.values() if c.last_polled]
        return {
            "repos": [
                {
                    **entry.model_dump(mode="json", exclude_none=True),
                    "polled": entry.should_poll(settings.poll_only_actionable),
                    "last_polled": (
                        marks.get(entry.full_name).last_polled.isoformat()
                        if marks.get(entry.full_name).last_polled
                        else None
                    ),
                }
                for entry in book.repo
            ],
            "statuses": list(watchlist.STATUSES),
            "polled_count": len(polled),
            "last_poll": max(last).isoformat() if last else None,
            "events": log.count(),
            "subjects": len(state.subjects),
            "enabled": settings.enabled,
            "free_hours": settings.free_hours_local,
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }

    @app.get("/api/inbox")
    def inbox() -> dict[str, Any]:
        settings = get_settings()
        log = _log()
        state = derive.derive(log.read())
        book = watchlist.load()
        repos = [
            e.full_name for e in book.repo if e.should_poll(settings.poll_only_actionable)
        ] or None

        found = derive.opportunities(
            state,
            stale_assignment_days=settings.stale_assignment_days,
            abandoned_pr_days=settings.abandoned_pr_days,
            repos=repos,
        )
        moves = derive.recent_transitions(state, within_hours=settings.transition_hours)
        titles = {s.key: (s.title, s.url) for s in state.subjects.values()}
        built = notify.build_digest(
            found, moves, sent=set(), max_items=200, titles=titles
        )
        dismissed = _dismissed(log)
        items = [
            {
                "key": item.key,
                "kind": item.kind,
                "headline": notify.HEADLINES.get(item.kind, item.kind),
                "repo": item.repo,
                "number": item.number,
                "title": item.title,
                "url": item.url,
                "note": item.note,
                "idle_days": item.idle_days,
            }
            for item in built.items
            if item.key not in dismissed
        ]
        return {"items": items, "dismissed": len(dismissed)}

    @app.get("/api/repo/{owner}/{name}")
    def repo_detail(owner: str, name: str) -> dict[str, Any]:
        full_name = f"{owner}/{name}"
        log = _log()
        entry = watchlist.load().find(full_name)
        probes = [e for e in log.read(kinds=[PROBE_COMPLETED]) if e.repo == full_name]
        probes.sort(key=lambda e: e.observed_at)
        history = [
            {"at": e.observed_at.isoformat(), "verdict": e.payload.get("verdict")}
            for e in probes
        ]
        latest = probes[-1] if probes else None
        state = derive.derive(log.read(repo=full_name))
        settings = get_settings()
        return {
            "entry": entry.model_dump(mode="json", exclude_none=True) if entry else None,
            "health": (latest.payload.get("health") if latest else None),
            "probed_at": latest.observed_at.isoformat() if latest else None,
            "history": history,
            "open_subjects": len(state.open_subjects()),
            "opportunities": [
                {
                    "kind": o.kind,
                    "number": o.number,
                    "title": o.title,
                    "url": o.url,
                    "idle_days": o.idle_days,
                    "note": o.note,
                }
                for o in derive.opportunities(
                    state,
                    stale_assignment_days=settings.stale_assignment_days,
                    abandoned_pr_days=settings.abandoned_pr_days,
                    repos=[full_name],
                )
            ],
        }

    @app.post("/api/probe")
    def probe(request: RepoRequest) -> dict[str, Any]:
        def work() -> dict[str, Any]:
            settings = get_settings()
            try:
                assert_enabled(settings.enabled)
            except SafetyError as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
            try:
                with GitHubClient(
                    deadline_seconds=settings.probe_deadline_seconds
                ) as client:
                    health = run_probe(client, request.repo, max_pages=settings.probe_pages)
                    spent = client.points_spent
            except Exception as exc:
                raise _as_http_error(exc, f"probing {request.repo}") from exc

            book = watchlist.load()
            existing = book.find(health.full_name)
            entry = _entry_from(health, existing, request.why)
            book.upsert(entry)
            watchlist.save(book)
            _log().append([_probe_event(health)])
            return {"health": _health_json(health), "entry": entry.model_dump(mode="json"),
                    "points_spent": spent}

        return _run_exclusive(work)

    @app.post("/api/poll")
    def poll() -> dict[str, Any]:
        def work() -> dict[str, Any]:
            settings = get_settings()
            book = watchlist.load()
            targets = [e for e in book.repo if e.should_poll(settings.poll_only_actionable)]
            try:
                assert_enabled(settings.enabled)
                assert_repo_cap([t.full_name for t in targets], settings.max_poll_repos)
            except SafetyError as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
            if not targets:
                return {"polled": 0, "written": 0, "note": "No repos are marked green yet."}

            marks = cursors.load()
            fresh, unchanged, errors = [], 0, []
            free = 0
            try:
                with GitHubClient(
                    deadline_seconds=settings.poll_deadline_seconds
                ) as client:
                    for result in poll_all(
                        client, [t.full_name for t in targets], marks, settings.poll_per_page
                    ):
                        if result.error:
                            errors.append(f"{result.repo}: {result.error}")
                        elif result.unchanged:
                            unchanged += 1
                        else:
                            fresh.extend(result.events)
                    free = client.rest_not_modified
            except Exception as exc:
                # Whatever was fetched before the failure is still worth keeping, so the
                # log is written below either way rather than thrown away.
                if not fresh:
                    raise _as_http_error(exc, "checking for changes") from exc
                errors.append(str(exc))

            written = _log().append(fresh)
            cursors.save(marks)
            return {
                "polled": len(targets),
                "written": len(written),
                "unchanged": unchanged,
                "free_requests": free,
                "errors": errors,
            }

        return _run_exclusive(work)

    @app.post("/api/mark")
    def mark(request: MarkRequest) -> dict[str, Any]:
        if request.status not in watchlist.STATUSES:
            raise HTTPException(400, f"Unknown status: {request.status}")
        book = watchlist.load()
        entry = book.find(request.repo)
        if entry is None:
            raise HTTPException(404, f"{request.repo} is not on the watchlist")
        entry.status = request.status
        entry.setup = request.setup or entry.setup
        entry.test = request.test or entry.test
        entry.notes = request.notes or entry.notes
        if request.minutes is not None:
            entry.cold_start_minutes = request.minutes
        if request.poll is not None:
            entry.poll = request.poll
        book.upsert(entry)
        watchlist.save(book)
        return entry.model_dump(mode="json", exclude_none=True)

    @app.delete("/api/repo/{owner}/{name}")
    def remove(owner: str, name: str) -> dict[str, bool]:
        book = watchlist.load()
        if not book.remove(f"{owner}/{name}"):
            raise HTTPException(404, "Not on the watchlist")
        watchlist.save(book)
        return {"removed": True}

    @app.post("/api/intent")
    def intent(request: IntentRequest) -> dict[str, Any]:
        """Snooze or dismiss an inbox item.

        Recorded as a notification event, which is how the digest already avoids
        repeating itself - so dismissing here also stops it reaching Discord.
        """
        if request.action not in ("dismiss", "snooze"):
            raise HTTPException(400, "Unknown action")
        event = Event.make(
            kind=notify.NOTIFICATION_SENT,
            repo=request.key.split("#")[0],
            subject=f"dashboard:{request.action}",
            occurred_at=datetime.now(UTC),
            payload={"channel": "dashboard", "keys": [request.key], "action": request.action},
        )
        _log().append([event])
        return {"key": request.key, "action": request.action}

    return app


# uvicorn's reloader needs an import string rather than an instance, so the app has to
# exist at module scope. Construction only registers routes; nothing here touches the
# network or the filesystem.
app = create_app()


def _dismissed(log: EventLog) -> set[str]:
    return notify.already_sent(log.read())


def _probe_event(health: metrics.RepoHealth) -> Event:
    label, reasons = metrics.verdict(health)
    return Event.make(
        kind=PROBE_COMPLETED,
        repo=health.full_name,
        subject="probe",
        occurred_at=datetime.now(UTC),
        # The whole card, so the dashboard can show a repository without spending rate
        # limit every time someone clicks on it. Re-probing stays a deliberate act.
        payload={"verdict": label, "reasons": reasons, "health": _health_json(health)},
    )


def _entry_from(
    health: metrics.RepoHealth, existing: watchlist.WatchedRepo | None, why: str
) -> watchlist.WatchedRepo:
    label, reasons = metrics.verdict(health)
    entry = (
        existing.model_copy()
        if existing
        else watchlist.WatchedRepo(full_name=health.full_name)
    )
    entry.full_name = health.full_name
    entry.why = why or entry.why
    entry.last_probed = datetime.now(UTC)
    entry.verdict = label
    entry.verdict_reasons = reasons
    entry.outsider_merge_rate = round(health.outsider_merge_rate.point, 3)
    entry.cold_merges = health.cold_merges
    entry.newness_sufficient = health.newness_sufficient
    entry.newness_point = round(health.newness.point, 4)
    entry.newness_lower = round(health.newness.lower, 4)
    entry.maintainer_utc_offset = health.maintainer_utc_offset
    if existing is None and label in (metrics.DEAD, metrics.TRAP):
        entry.status = "rejected"
    return entry
