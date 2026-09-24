"""scout command line.

Phase 1 is read-only against GitHub, by design. Nothing here posts a comment, opens a
pull request, or claims an issue. The point of this phase is to find out which
repositories are worth your time before any of that exists.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from scout import cursors, derive, metrics, notify, watchlist
from scout import enrich as enrichment
from scout.config import get_settings
from scout.events import EventLog
from scout.github import GitHubClient, GitHubError, NotFound
from scout.metrics import RepoHealth
from scout.poll import poll_all
from scout.probe import probe as run_probe
from scout.safety import SafetyError, assert_enabled, assert_repo_cap

app = typer.Typer(add_completion=False, help="Find repositories worth contributing to.")
console = Console()

VERDICT_STYLE = {
    metrics.DEAD: "dim",
    metrics.TRAP: "bold red",
    metrics.THIN: "yellow",
    metrics.VIABLE: "cyan",
    metrics.GOOD: "bold green",
}


def _fmt_hours(hours: float | None) -> str:
    if hours is None:
        return "-"
    if hours < 48:
        return f"{hours:.0f}h"
    return f"{hours / 24:.0f}d"


def _interval_bar(lower: float, upper: float, scale: float = 0.30, width: int = 24) -> str:
    """The confidence interval as a bar. A wide band reads as "we cannot tell" at a
    glance, which no amount of printing 3.4%-26.1% ever achieves."""
    lo = min(int(lower / scale * width), width - 1)
    hi = min(max(int(upper / scale * width), lo + 1), width)
    return f"[dim]{'.' * lo}[/][cyan]{'=' * (hi - lo)}[/][dim]{'.' * (width - hi)}[/]"


def _card(health: RepoHealth) -> Panel:
    label, reasons = metrics.verdict(health)
    style = VERDICT_STYLE[label]

    table = Table.grid(padding=(0, 2))
    table.add_column(style="dim", justify="right")
    table.add_column()

    table.add_row("stars", f"{health.stars:,}  ({health.language or 'unknown'})")
    table.add_row("last push", f"{health.days_since_push}d ago")
    table.add_row("open", f"{health.open_issues:,} issues / {health.open_prs:,} PRs")
    table.add_row("", "")
    table.add_row(
        "merges from outside",
        f"{health.outsider_merge_rate.point:.0%} of {health.merged_sample} merged PRs",
    )
    newness = health.newness
    if health.newness_sufficient:
        table.add_row(
            "somebody's first merge",
            f"[bold]{newness.point:.1%}[/]  {_interval_bar(newness.lower, newness.upper)}  "
            f"[dim]{newness.lower:.1%}-{newness.upper:.1%}[/]",
        )
        table.add_row(
            "",
            f"[dim]{newness.successes} of {newness.total} scored merges over "
            f"{health.newness_scored_days:.0f}d (the number that matters)[/]",
        )
    else:
        table.add_row("somebody's first merge", f"[yellow]cannot tell[/] - {health.newness_note}")
    if health.bots_excluded:
        table.add_row("", f"[dim]{health.bots_excluded} bot merges excluded[/]")
    table.add_row("outsider PR merge time", _fmt_hours(
        None if health.median_days_to_merge_outsider is None
        else health.median_days_to_merge_outsider * 24
    ))
    table.add_row("", f"[dim]{health.merged_coverage.describe()}[/]")
    table.add_row("", "")
    table.add_row(
        "reply to outsider issues",
        f"median {_fmt_hours(health.median_hours_to_maintainer_reply)}, "
        f"{health.unanswered_outsider_issues}/{health.outsider_issues} never answered",
    )
    table.add_row(
        "",
        f"[{'red' if health.maintainer_comments_seen == 0 else 'dim'}]"
        f"{health.maintainer_comments_seen} from a maintainer, "
        f"{health.comments_seen} comments seen in all[/]",
    )
    contest = health.beginner_contest_minutes
    table.add_row(
        "beginner issue claimed in",
        "-"
        if contest is None
        else f"median {contest:.0f} min  ({health.beginner_issues_sampled} sampled)",
    )
    table.add_row("", f"[dim]{health.issue_coverage.describe()}[/]")
    table.add_row("", "")
    table.add_row("maintainer clock", health.timezone_note)
    overlap = health.free_hour_overlap
    table.add_row(
        "active in your free hours",
        "-" if overlap is None else f"{overlap:.0%} of their activity",
    )

    if health.opportunities:
        table.add_row("", "")
        table.add_row("open now", f"[bold]{len(health.opportunities)}[/] uncontested items")
        for item in health.opportunities[:5]:
            table.add_row(
                f"  {item.kind}",
                f"#{item.number} idle {item.idle_days}d - {item.title[:58]}",
            )

    body = "\n".join(f"  {r}" for r in reasons)
    return Panel(
        table,
        title=f"[{style}]{label}[/]  {health.full_name}",
        subtitle=f"[{style}]{body.strip()}[/]",
        border_style=style,
    )


def _log() -> EventLog:
    return EventLog(get_settings().events_dir)


def _poll_targets(book: watchlist.Watchlist) -> list[watchlist.WatchedRepo]:
    only = get_settings().poll_only_actionable
    return [entry for entry in book.repo if entry.should_poll(only)]


def _client() -> GitHubClient:
    try:
        return GitHubClient()
    except GitHubError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc


def _entry_from(health: RepoHealth, existing: watchlist.WatchedRepo | None, why: str):
    label, reasons = metrics.verdict(health)
    entry = existing.model_copy() if existing else watchlist.WatchedRepo(
        full_name=health.full_name
    )
    entry.full_name = health.full_name
    entry.why = why or entry.why
    entry.last_probed = datetime.now(UTC)
    entry.verdict = label
    entry.verdict_reasons = reasons
    entry.outsider_merge_rate = round(health.outsider_merge_rate.point, 3)
    entry.newness_lower = round(health.newness.lower, 4)
    entry.maintainers = health.maintainers
    entry.newness_sufficient = health.newness_sufficient
    entry.newness_point = round(health.newness.point, 4)
    entry.cold_merges = health.cold_merges
    entry.maintainer_utc_offset = health.maintainer_utc_offset
    if existing is None and label in (metrics.DEAD, metrics.TRAP):
        entry.status = "rejected"
    return entry


@app.command()
def probe(
    repo: str,
    pages: int = typer.Option(
        0, "--pages", help="Look deeper. Default comes from SCOUT_PROBE_PAGES."
    ),
):
    """Score one repository on whether it will merge work from a stranger."""
    with _client() as client:
        try:
            health = run_probe(client, repo, max_pages=pages or get_settings().probe_pages)
        except NotFound:
            console.print(f"[red]no such repository: {repo}[/]")
            raise typer.Exit(1) from None
        except GitHubError as exc:
            console.print(f"[red]{exc}[/]")
            raise typer.Exit(1) from exc
        console.print(_card(health))
        console.print(
            f"[dim]{client.points_spent} rate-limit points spent, "
            f"{client.points_remaining} left this hour. `scout add` to keep it.[/]"
        )


@app.command()
def add(repo: str, why: str = typer.Option("", "--why", help="Why this one, in a sentence")):
    """Probe a repository and put it on the watchlist."""
    with _client() as client:
        try:
            health = run_probe(client, repo, max_pages=get_settings().probe_pages)
        except NotFound:
            console.print(f"[red]no such repository: {repo}[/]")
            raise typer.Exit(1) from None
    console.print(_card(health))

    book = watchlist.load()
    entry = _entry_from(health, book.find(health.full_name), why)
    book.upsert(entry)
    watchlist.save(book)
    console.print(f"[green]{entry.full_name} added as '{entry.status}'[/]")
    if entry.status == "candidate":
        console.print(
            "[dim]next: build it and get the test suite green, then "
            f"`scout mark {entry.full_name} green --setup ... --test ...`[/]"
        )


@app.command(name="list")
def list_repos():
    """Show the watchlist, best first. Reads the cached probe, no network."""
    book = watchlist.load()
    if not book.repo:
        console.print("[dim]watchlist empty - `scout add owner/repo`[/]")
        return

    table = Table(box=None, pad_edge=False)
    table.add_column("verdict")
    table.add_column("status")
    table.add_column("repo")
    table.add_column("outside", justify="right")
    table.add_column("1st", justify="right")
    table.add_column("cold start", justify="right")
    table.add_column("why", style="dim")

    ordered = sorted(
        book.repo,
        key=lambda r: (metrics.RANK.get(r.verdict, 0), r.outsider_merge_rate or 0),
        reverse=True,
    )
    for entry in ordered:
        style = VERDICT_STYLE.get(entry.verdict, "dim")
        table.add_row(
            f"[{style}]{entry.verdict or '?'}[/]",
            entry.status,
            entry.full_name,
            "-" if entry.outsider_merge_rate is None else f"{entry.outsider_merge_rate:.0%}",
            "-" if entry.cold_merges is None else str(entry.cold_merges),
            "-" if entry.cold_start_minutes is None else f"{entry.cold_start_minutes}m",
            entry.why[:44],
        )
    console.print(table)

    green = sum(1 for r in book.repo if r.actionable)
    console.print(
        f"\n[dim]{green} of {len(book.repo)} are buildable and worth an alert. "
        "Target is five.[/]"
    )


@app.command()
def refresh():
    """Re-probe every repository on the watchlist and report what changed.

    Everything, including rejected ones. A repo rejected by a scoring rule that has
    since been corrected must be able to come back, and the verdict standing in its way
    is exactly the one no longer trusted. At four points a repo against five thousand an
    hour, there is nothing to save by skipping them.
    """
    settings = get_settings()
    book = watchlist.load()
    cutoff = datetime.now(UTC) - timedelta(days=settings.reprobe_after_days)
    live = [r for r in book.repo if not r.last_probed or r.last_probed < cutoff]
    skipped = len(book.repo) - len(live)
    if skipped:
        console.print(
            f"[dim]{skipped} probed in the last {settings.reprobe_after_days} days, "
            "left alone. `scout probe <repo>` to force one.[/]"
        )
    if not live:
        console.print("[dim]nothing to refresh[/]")
        return

    with _client() as client:
        for entry in live:
            try:
                health = run_probe(client, entry.full_name, max_pages=settings.probe_pages)
            except GitHubError as exc:
                console.print(f"[red]{entry.full_name}: {exc}[/]")
                continue
            updated = _entry_from(health, entry, entry.why)
            # A refresh reports a downgrade; it never silently rejects something you chose.
            if entry.verdict and updated.verdict != entry.verdict:
                console.print(
                    f"[yellow]{entry.full_name}: {entry.verdict} -> {updated.verdict}[/] "
                    f"({'; '.join(updated.verdict_reasons)})"
                )
            updated.status = entry.status
            cleared = updated.verdict not in (metrics.DEAD, metrics.TRAP)
            if entry.status == "rejected" and cleared:
                # Back to the start of the funnel, not straight to green: clearing the
                # verdict says it is worth looking at, not that you can build it.
                updated.status = "candidate"
                console.print(
                    f"[green]{entry.full_name}: no longer rejected[/] - back to candidate"
                )
            book.upsert(updated)
            if health.opportunities:
                console.print(
                    f"[cyan]{entry.full_name}[/]: "
                    f"{len(health.opportunities)} uncontested items"
                )
    watchlist.save(book)
    console.print(f"[dim]{client.points_spent} points spent[/]")


@app.command()
def mark(
    repo: str,
    status: str = typer.Argument(..., help=" | ".join(watchlist.STATUSES)),
    setup: str = typer.Option("", "--setup", help="Command that installs dependencies"),
    test: str = typer.Option("", "--test", help="Command that runs the test suite"),
    minutes: int = typer.Option(None, "--minutes", help="Cold start, measured not guessed"),
    notes: str = typer.Option("", "--notes"),
):
    """Record what you learned building a repository. This is the field that decides
    whether an alert about it is actionable."""
    if status not in watchlist.STATUSES:
        console.print(f"[red]status must be one of: {', '.join(watchlist.STATUSES)}[/]")
        raise typer.Exit(1)

    book = watchlist.load()
    entry = book.find(repo)
    if entry is None:
        console.print(f"[red]{repo} is not on the watchlist[/]")
        raise typer.Exit(1)

    entry.status = status
    entry.setup = setup or entry.setup
    entry.test = test or entry.test
    entry.notes = notes or entry.notes
    if minutes is not None:
        entry.cold_start_minutes = minutes
    if status == "green" and not entry.test:
        console.print("[yellow]marked green with no --test command recorded[/]")
    book.upsert(entry)
    watchlist.save(book)
    console.print(f"[green]{entry.full_name} -> {status}[/]")


@app.command()
def rm(repo: str):
    """Drop a repository from the watchlist."""
    book = watchlist.load()
    if not book.remove(repo):
        console.print(f"[red]{repo} is not on the watchlist[/]")
        raise typer.Exit(1)
    watchlist.save(book)
    console.print(f"[green]{repo} removed[/]")


@app.command()
def poll(
    dry_run: bool = typer.Option(False, "--dry-run", help="Fetch, report, write nothing"),
    all_repos: bool = typer.Option(False, "--all", help="Include repos not yet green"),
):
    """Fetch what changed on the watchlist and append observations to the log.

    Safe to run twice: the second run writes 0. Observations are identified by the
    version of the thing they saw, so an overlapping poll dedupes itself away.
    """
    settings = get_settings()
    book = watchlist.load()
    targets = book.repo if all_repos else _poll_targets(book)
    targets = [t for t in targets if t.poll and t.status != "rejected"]

    try:
        assert_enabled(settings.enabled)
        assert_repo_cap([t.full_name for t in targets], settings.max_poll_repos)
    except SafetyError as exc:
        console.print(f"[yellow]{exc}[/]")
        raise typer.Exit(0) from exc

    if not targets:
        console.print(
            "[dim]nothing to poll - mark a repo green, or pass --all[/]"
        )
        return

    marks = cursors.load()
    with _client() as client:
        try:
            results = poll_all(
                client, [t.full_name for t in targets], marks, settings.poll_per_page
            )
        except SafetyError as exc:
            # Whatever was fetched before the floor was hit is still worth keeping.
            console.print(f"[yellow]{exc}[/]")
            cursors.save(marks)
            raise typer.Exit(0) from exc

    fresh: list = []
    for result in results:
        if result.error:
            console.print(f"[red]{result.repo}: {result.error}[/]")
        elif result.unchanged:
            console.print(f"[dim]{result.repo}: unchanged (304, free)[/]")
        else:
            fresh.extend(result.events)
            console.print(f"[cyan]{result.repo}[/]: {len(result.events)} observations")

    if dry_run:
        console.print(f"[yellow]dry run - {len(fresh)} events not written[/]")
        return

    written = _log().append(fresh)
    cursors.save(marks)
    console.print(
        f"[green]{len(written)} new[/] of {len(fresh)} observations, "
        f"{client.rest_requests} requests, {client.rest_not_modified} free, "
        f"{client.rest_remaining} left this hour"
    )


@app.command()
def enrich():
    """Ask GitHub what the listing cannot say about this week's unassigned issues.

    Linked pull requests and comment authors, for candidates only - a handful of requests.
    Writes observations to the log; decides nothing. Run it before the digest.
    """
    settings = get_settings()
    try:
        assert_enabled(settings.enabled)
    except SafetyError as exc:
        console.print(f"[yellow]{exc}[/]")
        raise typer.Exit(0) from exc

    log = _log()
    state = derive.derive(log.read())
    book = watchlist.load()
    repos = [entry.full_name for entry in _poll_targets(book)] or None

    with _client() as client:
        events, problems = enrichment.enrich(
            client,
            state,
            max_age_days=settings.fresh_max_age_days,
            limit=settings.fresh_max_candidates,
            batch_size=settings.fresh_batch_size,
            recheck_after_hours=settings.fresh_recheck_after_hours,
            repos=repos,
        )

    written = log.append(events)
    console.print(
        f"[green]{len(written)} new[/] of {len(events)} enriched, "
        f"{client.points_spent} rate-limit points spent"
    )
    for problem in problems:
        console.print(f"[yellow]{problem}[/]")
    # Partial failure is still progress: what came back is in the log. Exit non-zero only
    # when nothing did, so a scheduled run says so without losing the good batches.
    if problems and not written:
        raise typer.Exit(1)


@app.command()
def digest(send: bool = typer.Option(False, "--send", help="POST it to Discord")):
    """Build the evening digest from derived state. Prints it unless --send is given."""
    settings = get_settings()
    try:
        assert_enabled(settings.enabled)
    except SafetyError as exc:
        console.print(f"[yellow]{exc}[/]")
        raise typer.Exit(0) from exc
    log = _log()
    state = derive.derive(log.read())
    book = watchlist.load()
    repos = [entry.full_name for entry in _poll_targets(book)] or None

    found = derive.opportunities(
        state,
        stale_assignment_days=settings.stale_assignment_days,
        abandoned_pr_days=settings.abandoned_pr_days,
        fresh_max_age_days=settings.fresh_max_age_days,
        accepting_labels=settings.accepting_label_list,
        repos=repos,
        maintainers={e.full_name: e.maintainers for e in book.repo if e.maintainers},
    )
    moves = derive.recent_transitions(state, within_hours=settings.transition_hours)
    titles = {s.key: (s.title, s.url) for s in state.subjects.values()}

    built = notify.build_digest(
        found,
        moves,
        sent=notify.already_sent(log.read()),
        max_items=settings.digest_max_items,
        titles=titles,
    )

    if built.empty:
        # "nothing new" was true and useless: it looked the same whether the log was empty,
        # nothing was green, or there was simply no work today.
        if not state.subjects:
            if not _poll_targets(book):
                console.print(
                    "[dim]nothing to send: no repository is green or active, so nothing has "
                    "been polled. `scout mark <repo> green` first.[/]"
                )
            else:
                console.print(
                    "[dim]nothing to send: no observations in the log yet. "
                    "Run `scout poll` first.[/]"
                )
        else:
            console.print(
                f"[dim]nothing new: {len(state.subjects):,} issues and PRs observed, "
                f"no uncontested work found, {built.skipped} already sent[/]"
            )
        return

    for item in built.items:
        console.print(
            f"[bold]{item.repo}#{item.number}[/] "
            f"[dim]{notify.HEADLINES.get(item.kind, item.kind)}[/] - {item.title[:60]}"
        )
        console.print(f"  [dim]{item.note}  {item.url}[/]")
    if built.skipped:
        console.print(f"[dim]{built.skipped} already sent, not repeated[/]")

    if not send:
        console.print("")
        console.print("[dim]--send to post to Discord[/]")
        return

    try:
        messages = notify.send(
            built,
            webhook_url=settings.discord_webhook_url,
            bot_token=settings.discord_bot_token,
            channel_id=settings.discord_channel_id,
            state_dir=settings.state_dir,
            tz=settings.display_tz,
        )
    except notify.SendFailed as exc:
        console.print(f"[red]{exc}[/]")
        went = built.only(exc.delivered)
        if not went.empty:
            # Recorded, or the next digest sends these again. digest.yml commits this even
            # though the step fails.
            log.append([notify.sent_event(went)])
            console.print(
                f"[yellow]{len(went.items)} of {len(built.items)} reached Discord before it "
                "stopped, recorded as sent:[/]"
            )
            for item in went.items:
                console.print(f"  [dim]{item.repo}#{item.number}[/]")
        raise typer.Exit(1) from exc
    except (ValueError, RuntimeError) as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc
    log.append([notify.sent_event(built)])
    how = "as the bot, in a thread per repo" if settings.posts_as_bot else "by webhook"
    console.print(f"[green]sent {len(built.items)} items {how}[/] ({messages} messages)")


@app.command(name="events")
def events_cmd():
    """Event counts by kind, and what the log costs."""
    log = _log()
    counts: dict[str, int] = {}
    repos: dict[str, int] = {}
    for event in log.read():
        counts[event.kind] = counts.get(event.kind, 0) + 1
        repos[event.repo] = repos.get(event.repo, 0) + 1

    if not counts:
        console.print("[dim]log empty - `scout poll`[/]")
        return

    table = Table(box=None, pad_edge=False)
    table.add_column("kind")
    table.add_column("events", justify="right")
    for kind, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        table.add_row(kind, f"{count:,}")
    console.print(table)

    size = sum(shard.stat().st_size for shard in log.shards())
    console.print("")
    console.print(
        f"[dim]{sum(counts.values()):,} events across {len(log.shards())} shards, "
        f"{size / 1024:.0f} KB, {len(repos)} repos[/]"
    )


@app.command()
def replay(
    check: bool = typer.Option(False, "--check", help="Derive twice and diff the results"),
):
    """Rebuild derived state from the log. Derived state is disposable; this proves it."""
    log = _log()
    first = derive.derive(log.read())

    if check:
        second = derive.derive(log.read())
        same = first.subjects == second.subjects and first.transitions == second.transitions
        if not same:
            console.print("[red]derivation is not deterministic[/]")
            raise typer.Exit(1)
        console.print("[green]deterministic[/] - two rebuilds are identical")

    state = first
    open_now = state.open_subjects()
    console.print(
        f"derivation v{derive.DERIVATION_VERSION}: "
        f"{len(state.subjects):,} subjects ({len(open_now):,} open), "
        f"{len(state.transitions):,} transitions"
    )
    found = derive.opportunities(state)
    if found:
        counts: dict[str, int] = {}
        for item in found:
            counts[item.kind] = counts.get(item.kind, 0) + 1
        console.print(
            "opportunities: " + ", ".join(f"{n} {k}" for k, n in sorted(counts.items()))
        )


@app.command()
def serve(
    port: int = typer.Option(8765, "--port"),
    host: str = typer.Option("127.0.0.1", "--host", help="Localhost only by default"),
    reload: bool = typer.Option(
        False, "--reload", help="Restart when the code changes, for working on scout"
    ),
):
    """Open the dashboard: triage inbox, watchlist funnel, repo detail.

    The CLI handles one repository at a time. This is for the other two jobs - deciding
    what to spend an evening on, and comparing repositories against each other.
    """
    import uvicorn

    from scout import __version__

    console.print(f"scout {__version__} is at [bold]http://{host}:{port}[/]  (ctrl-c to stop)")
    if not reload:
        # A long-running server holds the code it started with. Editing scout and
        # wondering why the dashboard disagrees is a confusing half hour.
        console.print("[dim]code changes need a restart, or use --reload[/]")
    uvicorn.run(
        "scout.server:app",
        host=host,
        port=port,
        log_level="warning",
        reload=reload,
        reload_dirs=[str(Path(__file__).parent)] if reload else None,
    )


def _check_discord_bot(settings) -> int:
    """Validate the bot token and channel without posting anything. Returns failures."""
    import httpx

    base = "https://discord.com/api/v10"
    headers = {"Authorization": f"Bot {settings.discord_bot_token}"}
    try:
        me = httpx.get(f"{base}/users/@me", headers=headers, timeout=15)
        if me.status_code == 401:
            console.print("discord: [red]bot token rejected[/] - reset it on the Bot tab")
            return 1
        me.raise_for_status()
        name = me.json().get("username", "?")
        channel = httpx.get(
            f"{base}/channels/{settings.discord_channel_id}", headers=headers, timeout=15
        )
    except httpx.HTTPError as exc:
        console.print(f"discord: [red]could not reach Discord[/]: {exc}")
        return 1
    if channel.status_code in (403, 404):
        console.print(
            f"discord: bot [bold]{name}[/] is valid but [red]cannot see channel "
            f"{settings.discord_channel_id}[/] - allow View Channel and Send Messages on "
            "that channel, or check the id"
        )
        return 1
    if channel.is_success:
        console.print(
            f"discord: bot [bold]{name}[/] can see "
            f"#{channel.json().get('name', '?')} - digests go to a thread per repo per day. "
            "It also needs Create Public Threads and Send Messages in Threads there"
        )
        return 0
    console.print(f"discord: [red]unexpected {channel.status_code}[/] checking the channel")
    return 1


@app.command()
def doctor(repo: str = typer.Option("", "--repo", help="Also try probing this one")):
    """Check the token, the budget, and the settings.

    REST and GraphQL are checked separately on purpose. A fine-grained token can pass
    one and fail the other, and a single combined "it works" hides exactly the case
    that is confusing to debug.
    """
    settings = get_settings()
    console.print(f"watchlist: {watchlist.path().resolve()}")
    console.print(f"languages: {', '.join(settings.language_list)}")
    start_hour, end_hour = settings.free_hours
    console.print(f"free hours: {start_hour:02d}:00-{end_hour:02d}:00 {settings.display_tz}")

    token = settings.token
    if not token:
        console.print("[red]no token[/] - put SCOUT_GITHUB_TOKEN in .env")
        console.print(f"[dim]looked for .env in {Path.cwd()}[/]")
        raise typer.Exit(1)
    console.print(
        f"token: {settings.token_kind}, {len(token)} chars, "
        f"starts {token[:11]}[dim]...[/]"
    )

    failures = 0
    with _client() as client:
        try:
            data = client.graphql("query { rateLimit { remaining resetAt } viewer { login } }")
            console.print(
                f"  graphql [green]ok[/] as [bold]{data['viewer']['login']}[/], "
                f"{data['rateLimit']['remaining']} points until {data['rateLimit']['resetAt']}"
            )
        except GitHubError as exc:
            failures += 1
            console.print(f"  graphql [red]failed[/]: {exc}")

        try:
            response = client.rest_conditional("/rate_limit")
            core = (response.body or {}).get("resources", {}).get("core", {})
            console.print(
                f"  rest    [green]ok[/], {core.get('remaining', '?')} requests left"
            )
        except GitHubError as exc:
            failures += 1
            console.print(f"  rest    [red]failed[/]: {exc}")

        if repo:
            try:
                health = run_probe(client, repo, max_pages=1)
                console.print(f"  probe   [green]ok[/] on {health.full_name}")
            except GitHubError as exc:
                failures += 1
                console.print(f"  probe   [red]failed[/] on {repo}: {exc}")

    if failures and settings.token_kind == "fine-grained":
        console.print("")
        console.print(
            "[yellow]Fine-grained tokens are declined by organisations that have not "
            "enabled them, and their permissions are per-repository.[/]"
        )
        console.print(
            "[dim]A classic token with the public_repo scope avoids both.[/]"
        )

    from scout.config import unknown_env_keys

    for key, suggestion in unknown_env_keys():
        failures += 1
        hint = f" - did you mean {suggestion}?" if suggestion else ""
        console.print(f"[red]{key} in .env is not a setting, so it is being ignored[/]{hint}")

    log = _log()
    console.print(f"event log: {log.count():,} events in {len(log.shards())} shards")
    console.print(
        f"safety: {'[green]enabled[/]' if settings.enabled else '[yellow]DISABLED[/]'}, "
        f"read-only, cap {settings.max_poll_repos} repos, "
        f"rate floor {settings.rate_limit_floor}"
    )
    log_dir = Path(settings.events_dir)
    if log_dir.is_dir():
        console.print(f"log: [green]{log_dir}[/] holds {_log().count():,} events")
    else:
        # The log is on the `data` branch, so a fresh clone has the code and none of the
        # history. Without this the first command just reports an empty log.
        console.print(
            f"log: [yellow]{log_dir} is missing[/] - the log lives on the `data` branch, "
            "add it with [bold]git worktree add data data[/]"
        )
        failures += 1
    hook = settings.discord_webhook_url
    if settings.posts_as_bot:
        failures += _check_discord_bot(settings)
    elif settings.discord_channel_id and not settings.discord_bot_token:
        console.print(
            "discord: [yellow]channel id set but no bot token[/] - posting by webhook, "
            "so no buttons"
        )
    else:
        state = "webhook configured, no buttons" if hook else "[yellow]nothing configured[/]"
        console.print(f"discord: {state}")
    if failures:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
