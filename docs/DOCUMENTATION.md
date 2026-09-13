# scout — documentation

Everything you need to run it. For *why* it works the way it does, see
[DECISIONS.md](DECISIONS.md).

---

## Contents

- [Setup](#setup)
- [Commands](#commands)
- [Configuring what gets polled](#configuring-what-gets-polled)
- [The dashboard](#the-dashboard)
- [Verdicts and metrics](#verdicts-and-metrics)
- [The event log](#the-event-log)
- [Polling and the digest](#polling-and-the-digest)
- [Discord](#discord)
- [Deployment](#deployment)
- [Settings reference](#settings-reference)
- [Safety](#safety)
- [Troubleshooting](#troubleshooting)

---

## Setup

### 1. A GitHub token

Settings → Developer settings → Personal access tokens → **Fine-grained tokens**.

- Repository access: **Public repositories (read-only)**
- Permissions: none

Scout never writes to GitHub, and refuses to at the client level, so it needs nothing
more. A classic token with `public_repo` also works but grants far more than necessary.

### 2. Install

```bash
cp .env.example .env      # put the token in SCOUT_GITHUB_TOKEN
uv sync
uv run scout doctor
```

`doctor` should report your login, a rate-limit budget near 5000, and the safety
settings. If the token line fails, nothing downstream will work.

### 3. Find repositories worth your time

```bash
uv run scout probe pola-rs/polars
```

Read the **somebody's first merge** line first — it is the only one that answers
"will this project merge a patch from me". Do this for ten to fifteen candidates.

```bash
uv run scout add astral-sh/ruff --why "I use it daily"
```

### 4. Build one, and record what it took

```bash
uv run scout mark astral-sh/ruff green --setup "cargo build" --test "cargo test" --minutes 9
```

**Nothing is polled until at least one repo is `green`.** An alert about a project you
cannot build wastes the evening, so scout will not send one.

### 5. Watch

```bash
uv run scout poll      # run twice; the second should report 0 new
uv run scout digest    # see what would be sent
uv run scout serve     # or browse it
```

---

## Commands

| Command | Flags | What it does |
|---|---|---|
| `doctor` | | Token, rate-limit budget, safety settings, log size |
| `probe <repo>` | `--pages N` | Score one repository. Saves nothing |
| `add <repo>` | `--why "..."` | Probe, then put it on the watchlist |
| `list` | | The watchlist, from cache, no network |
| `refresh` | | Re-probe everything and report what changed |
| `mark <repo> <status>` | `--setup` `--test` `--minutes` `--notes` | Record what building it took |
| `rm <repo>` | | Drop it from the watchlist |
| `poll` | `--dry-run` `--all` | Fetch changes, append observations |
| `digest` | `--send` | Build the digest; `--send` posts it to Discord |
| `events` | | Event counts by kind, log size |
| `replay` | `--check` | Rebuild derived state; `--check` proves it is deterministic |
| `serve` | `--port` `--host` | Open the dashboard |

`refresh` covers rejected repositories too. A repo rejected by a scoring rule that has
since been corrected has to be able to come back, and the verdict standing in its way is
the one no longer trusted.

---

## Configuring what gets polled

Three gates, all of which must pass:

1. **It is in `watchlist.toml`** — put there by `scout add`
2. **Its `status` is `green` or `active`** — because `SCOUT_POLL_ONLY_ACTIONABLE`
   defaults true
3. **Its `poll` field is true** — the per-repo mute

Plus a hard ceiling of `SCOUT_MAX_POLL_REPOS` across the whole list.

### The funnel

| Status | Meaning | Polled |
|---|---|---|
| `candidate` | Worth a look, not built yet | no |
| `building` | You are working out how to build it | no |
| `green` | Its test suite passes on your machine | **yes** |
| `active` | You have landed something here | **yes** |
| `rejected` | Ruled out; kept so it is not suggested again | no |

### watchlist.toml

Curated data, hand-edited and committed — not configuration, which is why secrets live
in the environment and this does not.

```toml
[[repo]]
full_name = "astral-sh/ruff"
status = "green"
why = "I use it daily"
poll = true
setup = "cargo build"
test = "cargo test"
cold_start_minutes = 9
verdict = "GOOD"
```

To mute a repo mid-release-week without losing what you learned, set `poll = false`.
To poll everything regardless of build status for one run, `scout poll --all`.

---

## The dashboard

```bash
uv run scout serve
```

<http://127.0.0.1:8765>, localhost only by default.

- **Inbox** — tonight's work, ranked, each with why nobody is racing for it. Hiding an
  item writes the same record the digest reads, so it also stops reaching Discord.
- **Watchlist** — the funnel in one row. The bottleneck is wherever the cards pile up.
- **Repo detail** — the full card, every metric expandable into a plain-English
  explanation.

Opening a repository costs nothing — it reads the last probe out of the event log.
Re-probing is a separate button, because it spends rate limit.

Every magnitude is drawn as a line against a scale rather than printed. A confidence
interval drawn as a band makes "cannot tell yet" visible before you read any numbers.

---

## Verdicts and metrics

| Verdict | Meaning |
|---|---|
| `DEAD` | Archived, issues disabled, or no push in 120 days |
| `TRAP` | Active and popular, but does not merge outsiders or answer them |
| `THIN` | Not enough evidence to say either way |
| `VIABLE` | Nothing disqualifying |
| `GOOD` | Newcomers reliably get merged |

`THIN` is a real answer. It means the sample cannot support a conclusion — usually
because the project moves faster than the page budget can follow. Probe deeper with
`--pages`.

### Somebody's first merge

The load-bearing metric: of the pull requests merged in the window, what share were the
author's first one here.

Derived from author logins and dates, **not** GitHub's `authorAssociation` — that field
describes how someone is associated today, so anyone who broke in months ago now reads as
an established contributor and their first merge vanishes from any historical window.

Reported as a range, never a bare number. See [DECISIONS.md](DECISIONS.md#the-metric)
for why.

### Everything else

| Metric | What it measures |
|---|---|
| Merges from outside | Share of merges by non-members. Survives a short sample well |
| Reply time to outsiders | Median wait for a first maintainer reply. Silence counts only after 72h |
| Beginner issue contest speed | Minutes before an outsider claims a `good first issue` |
| Active while you are free | Share of maintainer activity inside `SCOUT_FREE_HOURS_LOCAL` |
| Cold start | Minutes from clone to green suite. Recorded by hand |
| Coverage | How far back the sample actually reached |

---

## The event log

`events/YYYY-MM.jsonl`, append-only, committed to git.

GitHub's API returns **state, not events** — a poll says what an issue looks like now,
not what happened to it. So the log holds *observations*, and every transition is derived
by comparing consecutive ones.

Event identity is `hash(kind, repo, number, updated_at)` — what was seen, not when you
looked. Two overlapping polls write one row, which is what makes an overlapping cron
schedule safe.

```bash
uv run scout replay --check
```

Everything derived is disposable. `state/cursors.json` is deliberately **not** in the
log: an ETag is a note about where this machine got to, not a fact about the world.

---

## Polling and the digest

Two separate decisions:

- **Observing** every 15 minutes, so the log is accurate
- **Interrupting** once at 20:00 IST, when there is time to act

One repo per poll is usually one request that returns **304 and costs no rate limit at
all**. That works only because the URL is stable — a moving `since=` parameter would
invalidate the ETag every time, so the poller uses a fixed URL and stops reading at a
watermark instead.

Ten repos every fifteen minutes is forty requests an hour against a limit of five
thousand.

### What gets surfaced

Not beginner-labelled issues. Those are the most contested real estate on GitHub.

| Kind | Why nobody is racing for it |
|---|---|
| `unassigned` | Someone claimed it and let it go. Wanted work, now free |
| `unanswered-report` | An outsider's bug report with no reply. Reproduce it and you are a name maintainers know |
| `stale-assignment` | Assigned 21+ days, untouched |
| `abandoned-pr` | Open 30+ days, author gone, often 80% finished |

---

## Discord

Two levels, and the second is not optional if you want buttons.

### Notifications only — two minutes, no bot

Server Settings → Integrations → Webhooks → New Webhook → Copy URL →
`SCOUT_DISCORD_WEBHOOK_URL`. That is the whole setup.

### Buttons — needs the bot to post

A webhook you create in Discord's UI is **not owned by an application**, and Discord
ignores interactive components from those: *"Non-application-owned webhooks cannot send
interactive components."* No amount of formatting gets buttons onto a hand-made webhook's
messages. The bot has to post them.

**Install it to a server, not to your account.** Discord apps have two installation
contexts, and the portal's *Discord Provided Link* offers whichever are enabled. If
**User Install** is on, the link can produce a screen saying the app "wants to access your
Discord account" with *Create commands* and *Send you direct messages* — and no server
picker. That installs scout to you personally and it cannot post to a channel. Turn User
Install off under **Installation → Installation Contexts** and leave **Guild Install** on.

**Keep the bot private, and invite it with a generated URL.** With *Public Bot* turned off
on the Bot tab, Discord refuses a default install link ("Private application cannot have a
default authorization link"), so set **Installation → Install Link** to **None**. Then use
**OAuth2 → URL Generator**: scope `bot`, integration type Guild Install, no permissions.
Open the generated URL; the correct screen says **Add to server** and asks which.

A bot is always added to a whole server, never one channel. To confine it, give it no
server-wide permissions at install and grant these on `#scout` alone, as a channel
permission override: **View Channel**, **Send Messages**, **Embed Links**, **Create Public
Threads** and **Send Messages in Threads**.

Set `SCOUT_DISCORD_BOT_TOKEN` and `SCOUT_DISCORD_CHANNEL_ID` and scout posts as the bot
instead, with an action row under each item. Leave them unset and it falls back to the
webhook.

**Layout: one thread per repository per day.** Components attach to a message, not to an
embed, so per-item buttons mean one message per item - which is a wall of messages if they
all land in the channel. Instead the channel gets a single line per repository per day,
such as `14 Sep - BerriAI/litellm (8)`, with a thread off it holding that repository's
items in rank order. Sending again on the same day adds to the existing thread and
updates the count rather than opening a second one; thread ids are kept in
`state/discord_threads.json`. The day is taken in `SCOUT_DISPLAY_TZ`, not UTC.

Webhook mode stays a single message: a plain webhook can neither start a thread in a text
channel nor carry buttons.

The Later and Not for me buttons need something listening, which is `worker/` — a
Cloudflare Worker that verifies Discord's signature and forwards the intent. Open on
GitHub is a link button and needs nothing at all.

---

## Deployment

| Need | Where | Why |
|---|---|---|
| Durable log | git | Free, versioned, and the log is the asset |
| Scheduled runs | GitHub Actions | Free on a public repo |
| Public HTTPS receiver | Cloudflare Worker | Discord needs an answer in three seconds |

`.github/workflows/poll.yml` runs every 15 minutes; `digest.yml` at 14:30 UTC.

Repository secrets: `SCOUT_GITHUB_TOKEN`, `SCOUT_DISCORD_WEBHOOK_URL`. Settings →
Actions → General → Workflow permissions must be **Read and write**, or the poll job
cannot commit the log.

**Use a public repository.** Public repos get unlimited Actions minutes; a private one
gets 2,000 a month, and a 15-minute cadence costs roughly 2,880. If you must keep it
private, change the cron to `*/30` and set a spending limit of zero.

Actions' scheduled workflows run late fairly often, which costs nothing here because the
digest is batched to the evening anyway. Note that GitHub disables schedules after 60
days of repository inactivity — the log commit on each poll is what keeps them alive.

---

## Settings reference

All environment variables, prefixed `SCOUT_`. Set them in `.env`.

| Variable | Default | Effect |
|---|---|---|
| `GITHUB_TOKEN` | — | Required |
| `GITHUB_LOGIN` | — | Your handle |
| `DISCORD_WEBHOOK_URL` | — | Required for `digest --send` |
| `FREE_HOURS_LOCAL` | `20-24` | Your free hours, for the overlap metric |
| `LANGUAGES` | `Python,Go,TypeScript` | For candidate search |
| `DISPLAY_TZ` | `Asia/Kolkata` | |
| **Probe** | | |
| `PROBE_PAGES` | `4` | Pages walked before giving up on the window |
| `LOOKBACK_DAYS` | `180` | The window every metric claims |
| `MERGED_PR_SAMPLE` | `100` | Rows per page (GitHub caps at 100) |
| `ISSUE_SAMPLE` | `60` | Issues per page |
| `UNANSWERED_AFTER_HOURS` | `72` | Before silence counts as neglect |
| `STALE_ASSIGNMENT_DAYS` | `21` | |
| `ABANDONED_PR_DAYS` | `30` | |
| `NEWNESS_BURN_IN_DAYS` | `14` | Establishes who was already known |
| `NEWNESS_MIN_BURN_IN_MERGES` | `30` | |
| `NEWNESS_MIN_SCORED_MERGES` | `40` | Below this, it refuses to judge |
| **Poll** | | |
| `POLL_ONLY_ACTIONABLE` | `true` | Restrict to `green`/`active` |
| `POLL_PER_PAGE` | `100` | |
| **Digest** | | |
| `DIGEST_MAX_ITEMS` | `8` | |
| `TRANSITION_HOURS` | `36` | How far back "just came free" looks |
| **Timeouts** | | |
| `TIMEOUT_SECONDS` | `30` | Per request |
| `PROBE_DEADLINE_SECONDS` | `90` | Whole probe |
| `POLL_DEADLINE_SECONDS` | `180` | Whole poll |
| **Safety** | | |
| `ENABLED` | `true` | Kill switch |
| `MAX_POLL_REPOS` | `25` | Refuses above this |
| `RATE_LIMIT_FLOOR` | `500` | Stops this far short of the limit |
| **Paths** | | |
| `WATCHLIST_PATH` | `./watchlist.toml` | |
| `EVENTS_DIR` | `./events` | |
| `STATE_DIR` | `./state` | |

---

## Safety

| Guard | Stops | Setting |
|---|---|---|
| Read-only enforcement | Any write to GitHub, including GraphQL `mutation` | Structural |
| Operation deadline | A retry storm blocking a caller for the better part of an hour | `PROBE_DEADLINE_SECONDS` |
| Rate-limit floor | Polling on until the token is throttled | `RATE_LIMIT_FLOOR` |
| Repo cap | A watchlist that quietly grew to 200 | `MAX_POLL_REPOS` |
| Kill switch | Everything, without touching a schedule | `ENABLED` |
| Single-flight lock | Two dashboard jobs racing and spending double | Structural |
| Job timeout | A hung workflow burning minutes | `timeout-minutes: 10` |

The read-only guard is structural: a client must be constructed `read_only=False` on
purpose to get past it, which no code path does.

---

## Troubleshooting

**`scout poll` says there is nothing to poll.**
No repository is `green` yet. Build one and `scout mark <repo> green`, or pass `--all`.

**The dashboard says it cannot reach GitHub.**
502 means the network failed, 504 means GitHub or the whole operation ran out of time,
429 means the token is being rate limited and will clear within the hour. All three are
transient; the log is never left half-written.

**A probe comes back `THIN` on a big repository.**
The project merges faster than the page budget can follow. `scout probe <repo> --pages
20`. Some repositories are simply too fast to judge on first-timer counts, and saying so
is the intended behaviour.

**`scout replay --check` says derivation is not deterministic.**
A bug. Open an issue with the output of `scout events`.

**The poll workflow fails on `git add events state`.**
The directories are missing. They ship with `.gitkeep` files; restore them.

**Two polls ran at once and I have duplicate rows.**
You do not. Event ids are content-derived, so derivation drops the duplicate. `scout
events` counts unique ids.
