<div align="center">

# scout

**Find open source projects that will actually merge your patch — then watch them for work nobody is racing for.**

[![Python](https://img.shields.io/badge/python-3.12+-1b2733?logo=python&logoColor=white)](https://www.python.org/)
[![Tests](https://img.shields.io/badge/tests-212%20passing-2c7a4b)](tests/)
[![Ruff](https://img.shields.io/badge/lint-ruff-37788a?logo=ruff&logoColor=white)](https://github.com/astral-sh/ruff)
[![License](https://img.shields.io/badge/license-MIT-37788a)](LICENSE)
[![GitHub access](https://img.shields.io/badge/GitHub_access-read--only-2c7a4b)](docs/DECISIONS.md#nothing-writes-to-github)

[Documentation](docs/DOCUMENTATION.md) · [Architecture](docs/ARCHITECTURE.md) · [Design decisions](docs/DECISIONS.md) · [Quick start](#quick-start)

</div>

---

A repository with 40,000 stars can have a busy issue tracker and still never merge a patch
from someone it does not already know. Nothing on its page tells you which kind it is.
Scout works it out in about four API calls, then watches the ones worth your time for work
that is not being raced for.

```
╭──────────────────────────── GOOD  pola-rs/polars ────────────────────────────╮
│                  stars  31,204  (Rust)                                       │
│                                                                              │
│    merges from outside  44% of 600 merged PRs                                │
│ somebody's first merge  14.2%  ..........======........  11.8%-17.0%         │
│                         85 of 427 scored merges over 95d                     │
│                                                                              │
│       maintainer clock  UTC-7 (Americas) - their morning is your evening     │
│               open now  2 uncontested items                                  │
│           abandoned-pr  #7755 idle 91d - Add lazy sink_ipc support           │
╰─ at least 11.8% of merges are someone's first (85/427 over 95d) ─────────────╯
```

## What it does

**Scores repositories on whether a stranger gets merged.** The load-bearing number is the
share of merged pull requests that were the author's *first* one there — reported as a
confidence interval, so a rate measured over twelve merges never masquerades as one
measured over six hundred.

**Watches the ones you can build.** A repository you cannot get to a passing test suite is
one you will never touch on a weeknight, so nothing is polled until you have built it and
said so.

**Surfaces work nobody is racing for.** Not labelled beginner issues — those are the most
contested real estate on GitHub. Instead: issues that just came free, unanswered bug
reports, stale assignments, and half-finished pull requests whose author walked away.

**Tells you when you can act on it.** One digest in the evening, not a notification every
time something moves.

## Quick start

```bash
cp .env.example .env      # add a fine-grained token: public repos, read-only
uv sync
uv run scout doctor
```

```bash
uv run scout probe pola-rs/polars     # score one repository
uv run scout add astral-sh/ruff --why "I use it daily"
uv run scout serve                    # or browse it at localhost:8765
```

Build one, record what it took, and it starts being watched:

```bash
uv run scout mark astral-sh/ruff green --setup "cargo build" --test "cargo test" --minutes 9
```

Full walkthrough in the [documentation](docs/DOCUMENTATION.md).

## The dashboard

`scout serve` opens three views on localhost: an inbox of tonight's work, the watchlist as
a funnel, and per-repository detail where every metric expands into a plain-English
explanation of what it measures.

Every magnitude is drawn as a line against a scale rather than printed, so "cannot tell
yet" is visible before you read a single number.

## How it works

```
GitHub REST ──▶ poller ──▶ events/*.jsonl ──▶ derivation ──▶ digest ──▶ Discord
 (304 = free)   15 min     append-only,       disposable,     20:00
                           committed to git   replayable
```

The log holds **observations**, not events — GitHub's API returns state, not history — and
every transition is derived by comparing consecutive snapshots. Event identity is what was
seen rather than when you looked, so overlapping polls deduplicate themselves and running
the poller twice writes nothing the second time.

How the pieces connect, with diagrams of each flow: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
Why they are built that way: [docs/DECISIONS.md](docs/DECISIONS.md).

## Read-only, by construction

Scout does not comment, claim, or open pull requests, and the client refuses to: every
call is checked, GraphQL bodies included, and a client has to be built `read_only=False`
on purpose to get past it. No code path does.

That is a deliberate design constraint rather than a missing feature. Maintainers began
gating their communities when automation started speaking on people's behalf, and a tool
meant to get you *into* those communities should not be able to do that by accident. When
claiming is added it stays behind a human tap for the same reason.

The rest is ordinary API usage: an authenticated read of public data, roughly forty
requests an hour against a limit of five thousand, with ETags honoured so most of them
cost nothing at all. Scheduled runs commit their results back to the repository — the same
shape as the many "git scraping" workflows that have run on GitHub Actions for years.

## Running it in the background

Two workflows, both free on a public repository: `poll.yml` every fifteen minutes,
`digest.yml` once in the evening. The event log lives in git, so there is no database to
run and no state to back up.

Buttons in Discord need a public HTTPS endpoint, which is what `worker/` is — a small
Cloudflare Worker that verifies Discord's signature and forwards the intent. It is
optional; the digest works without it.

See [deployment](docs/DOCUMENTATION.md#deployment) for setup, including why a public
repository matters for the Actions allowance.

## Tests

```bash
uv run pytest
```

212 tests, no network in any of them. Scoring is a pure function over recorded API
responses, so every verdict is testable without a token and there are no cassettes to go
stale.

## License

MIT
