# Design decisions

Why scout works the way it does. Most of these were made after something went wrong, and
the ones that were wrong the first time say so.

---

## The problem

Finding "beginner friendly" issues is not the bottleneck in contributing to open source.
Three other things are:

1. **Most popular repositories will not merge your patch.** They have a public issue
   tracker and a closed contributor list, and nothing on the repository page distinguishes
   the two.
2. **A repository you cannot build is one you cannot contribute to.** If getting to a
   passing test suite takes forty minutes, you will never do it on a weeknight.
3. **The race for labelled beginner issues is unwinnable** from a timezone where you are
   asleep or at work when they appear.

Scout answers the first mechanically, records the second once, and refuses to enter the
third.

---

## The metric

This is the part that changed most, and the reasoning matters more than the result.

### First attempt: count first-time contributors

GitHub stamps every pull request with an `authorAssociation`, one value of which is
`FIRST_TIME_CONTRIBUTOR`. The first version counted them:

```
TRAP if 20+ merges and zero first-timers
GOOD if at least 3 first-timers and 25% of merges from outside
```

It looked reasonable. It was wrong in three separate ways.

### Problem 1: an absolute count is meaningless without a denominator

Three first-timers in 180 days is:

- **10%** on a project merging 30 pull requests in that window — genuinely open
- **0.03%** on a project merging 9,000 — effectively closed

The rule passed both. So it was not merely too strict on fast projects; it would happily
**certify** a huge repository that merges three newcomers a year.

### Problem 2: a count of a rare event needs time, and the sample had none

Running it against `BerriAI/litellm` produced this:

```
TRAP  BerriAI/litellm
  merges from outside     80% of 100 merged PRs
  first-timers merged     0
                          100 rows spanning 2d of 180d
  reply to outsiders      60/60 never answered
                          60 rows spanning 3d of 180d
```

Both charges were artefacts of the sample.

The queries fetch a fixed number of rows, newest first. On a repository merging fifty
pull requests a day, a hundred rows is **two days**. In any two-day window on such a
project, nearly everyone who merges has merged before — they read as established
contributors, and the newcomers from three months ago are outside the sample entirely.
Zero first-timers in two days is not evidence of anything.

Meanwhile **80% of merges came from outside with a six-hour median merge time**, which is
close to the opposite of a closed shop.

The second charge was a separate bug: silence was counted as neglect with no minimum age,
so a busy tracker full of issues opened *hours* ago looked negligent by construction.

### The asymmetry that was missed

The first fix was a coverage gate: do not convict when the sample covers less than half
the window. It worked, but it was reasoning from the wrong principle — the original
argument had been "coverage should never change a verdict, because five first-timers in
nine days is stronger evidence than five across six months."

That is true, and only in one direction:

> **A rate survives a short window. A count of a rare event does not.**
>
> Finding N newcomers quickly is *stronger* evidence. Finding none quickly is *no*
> evidence.

A threshold crossed upward is robust to a small sample. The same threshold not crossed is
not. Treating both the same is the mistake.

### The fix: judge the interval, not the number

Every rate is now a proportion with a **Wilson score interval**, and verdicts read the
bound rather than the point estimate.

```
TRAP    upper bound < 2%      even optimistically, newcomers do not land here
GOOD    lower bound > 5%      they reliably do
THIN    interval wider than 20 points    the sample cannot settle it
VIABLE  otherwise
```

Why this is better, concretely:

| Observed | Point estimate | 95% interval | Verdict |
|---|---|---|---|
| 0 of 100 | 0% | 0% – 3.7% | not enough to convict |
| 0 of 1,000 | 0% | 0% – 0.4% | **TRAP** |
| 3 of 30 | 10% | 3.5% – 25.6% | **cannot tell** |
| 20 of 240 | 8.3% | 5.5% – 12.5% | **GOOD** |

The sample size does the arguing. Zero newcomers convicts only when scout has looked hard
enough for the absence to mean something, and that threshold is arrived at rather than
guessed.

Two consequences worth noting:

- **The coverage gate became unnecessary.** It was a proxy for "too small to conclude";
  the interval measures that directly and continuously.
- **The old `GOOD` rule was certifying on nothing.** Three of thirty has an honest
  interval of 3.5% to 25.6% — consistent with a nearly closed project and with a wide
  open one. It should never have been a verdict.

Wilson specifically, rather than the textbook normal approximation, because the normal
one breaks down exactly where this data lives — at proportions near zero, where it
returns negative lower bounds and intervals far too narrow to be honest.

### Problem 3: the underlying field does not mean what it says

This started as a suspicion and was settled by sampling BerriAI/litellm's public API.

| Sample | `MEMBER` / `OWNER` / `COLLABORATOR` found |
|---|---|
| 30 issue and pull request authors | 0 |
| 66 comments across 12 threads | 0 |
| 100 most recent comments repo-wide | 0 |

Not one, anywhere. Meanwhile `ishaan-jaff` and `yuneng-berri` both merged pull requests
during the window, so both certainly have write access.

The cause: **BerriAI has zero public organisation members.** `author_association` reports
`MEMBER` only when somebody has made their membership public, so on that repository the
field cannot name a maintainer at all. Scout had read that as "the maintainers ignore
1152 of 1152 issues" and returned `TRAP` on a project that merges outsiders' pull
requests in six hours.

Worth noting for anyone sampling this themselves: of the 29 `CONTRIBUTOR` comments, 27
were bots. Automation accrues contributor status like anybody else.

**The fix: merging is a permission.** Nobody without write access can do it, and
`PullRequest.mergedBy` is already in the query the probe makes, so the maintainer set
costs no extra request. It is unioned with the association, which stays correct wherever
it is readable.

That set decides who counts as a maintainer for reply time, for the activity clock, and
for keeping a maintainer's own stale pull request out of the work offered to you. The
poll has the same blind spot and cannot close it alone - the issues endpoint never says
who merged anything - so the probe records the set on the watchlist entry and derivation
reads it.

Re-probed afterwards, litellm went from `TRAP` to `VIABLE`: 70 maintainer comments found
where there had been none, reply time resolved to a median of two days, the clock
resolved to UTC-8, and two false opportunities disappeared.

### The same field, for first-timers

`authorAssociation` is documented as how the author **is** associated with the
repository — present tense. If it is computed when you read it rather than frozen at
merge time, then somebody who broke in six months ago reads as `CONTRIBUTOR` today and
their first merge disappears from every historical window.

Rather than depend on the answer, newness is now derived from data that cannot drift:
**for each merged pull request, was this the author's first one here?** Author logins and
dates are already in the response.

That introduces one problem of its own. Everybody appears for the first time at some
point in a sample, including ten-year maintainers, so the earliest stretch of the window
is spent only establishing who was already known — a **burn-in** — and nothing in it is
scored. If the sample cannot afford both a burn-in and enough merges after it, scout
refuses rather than returning a biased rate with a confident-looking interval around it.

`authorAssociation` is still reported, and **disagreement between the two is surfaced**.
It is the cheapest available evidence about whether the field can be trusted:

```
authorAssociation reports 0 first-timers while author history reports 14.2%
- the field describes today, not the merge
```

### Bots

Automation merges constantly and is never a newcomer. Leaving `dependabot` and friends in
the denominator quietly deflates every rate, worst on the busiest projects. GraphQL types
the author, so they are excluded by type rather than by guessing at names.

---

## Observing and interrupting are different decisions

The obvious design is: check often, notify on what you find. It produces a channel you
mute within a week.

The constraint that matters is not how fast scout learns something — it is when you can
act. Learning at 10:00 rather than 10:05 changes nothing if you are at work until seven.

So the two are separate knobs:

- **Poll every 15 minutes**, so the event log is accurate
- **Send one digest in the evening**, when there is time to act on it

Immediate interruption is reserved for a narrow class that earns it.

---

## The log holds observations, not events

GitHub's API returns **state, not events**. A poll tells you what an issue looks like
now, not what happened to it.

So the log stores *observations* — one snapshot of one subject at one moment — and every
transition ("just came free", "went stale") is derived by comparing consecutive ones.

Recording transitions directly would put conclusions in the log. A later correction to
how a conclusion is drawn could not then be replayed over history, which defeats the
point of keeping the log at all.

### Identity is what was seen, not when you looked

An event's id is `hash(kind, repo, number, updated_at)`. Two overlapping polls that both
see version N of issue 42 produce the same id and write one row.

This is what makes an overlapping cron schedule safe, and why running the poller twice
writes nothing the second time.

### Git rather than a database

The log is append-only JSONL committed to the repository. Git is already an append-only
log with durability and history; a database would add something to back up in exchange
for a few megabytes a year.

`state/cursors.json` is deliberately **not** in the log. An ETag is a note about where
this machine got to, not a fact about the world, and replay must not depend on it.

---

## Polling is nearly free, and the URL is why

`/repos/:owner/:repo/issues` returns pull requests too, so one request covers both — and
one ETag covers both. GitHub does not charge rate limit for a **304**, so checking an
idle repository costs nothing at all.

That only works with a **stable URL**. A moving `since=` parameter is part of the URL, so
it invalidates the ETag on every poll and throws away the one free thing on offer. The
poller uses a fixed URL and stops reading at a watermark instead.

Ten repositories every fifteen minutes is forty requests an hour against a limit of five
thousand. The rate limit was never the constraint.

---

## What gets surfaced, and what does not

Labelled beginner issues are deliberately **excluded**. They are the most contested
real estate on GitHub, the race is against people and bots watching the firehose, and it
cannot be won from a timezone where you are at work when they appear.

The work scout looks for is uncontested for structural reasons:

| Kind | Why it is quiet |
|---|---|
| `unassigned` | Someone wanted it enough to claim it, then let it go. No race, and the work is known to be wanted |
| `unanswered-report` | Reproducing a bug report is real work with no competition, and the fastest way to become a name maintainers recognise |
| `stale-assignment` | Assigned weeks ago and untouched. A polite "is this still being worked on?" has a high hit rate |
| `abandoned-pr` | Often eighty per cent finished. The cheapest merged pull request available |

A cross-reference check suppresses stale assignments that already have a pull request
pointing at them — suggesting you go and pester someone actively working is exactly the
noise scout exists to avoid.

---

## Nothing writes to GitHub

Every call is checked, including GraphQL bodies for `mutation`, and a client must be
constructed `read_only=False` on purpose to get past it. No code path does.

This is enforced structurally rather than by convention because the failure mode is
severe and quiet. An auto-commenting bot is indistinguishable from the behaviour that
made maintainers gate their communities in the first place, and a tool built to get you
*into* those communities should not be capable of doing it by accident.

When claiming is eventually built, it stays behind a human tap for the same reason.

---

## Deadlines, not just timeouts

A per-request timeout bounds one request. It does not bound an operation.

A probe is up to nine requests. Each can retry four times, sleeping between attempts, and
the sleep length on a rate limit comes from a `retry-after` header **GitHub chooses**.
Multiplied out, the original code could block a caller for the better part of an hour
with the dashboard spinner still turning.

So there is a deadline over the whole operation, checked before every request and before
every sleep, and `retry-after` is capped rather than honoured literally. A caller that
cannot wait should fail and be retried by whatever scheduled it.

Reached from the dashboard, this becomes a `504` with a readable message rather than a
hung request.

---

## Failures are answers

A `500` tells the person at the browser nothing and leaves them wondering what they
broke. Every failure the dashboard can produce has a cause worth naming:

| | |
|---|---|
| `404` | No such repository, or it is private |
| `429` | The token is rate limited; it clears within the hour |
| `502` | Could not reach GitHub |
| `504` | GitHub, or the whole operation, ran out of time |
| `500` | A bug in scout — and it names the exception |

The traceback still goes to the server log. Only the last case is a `500`, and it says so
explicitly rather than pretending the request was bad.

---

## Design of the dashboard

The CLI is good at one repository at a time. The dashboard exists for the two jobs it
cannot do: deciding what to spend an evening on, and comparing repositories against each
other. Re-rendering the probe card in a browser would have added nothing.

**Every magnitude is drawn as a measured line against a scale, never printed as a
number.** That follows from what scout is: a tool whose defining behaviour is refusing to
state what it cannot support. A confidence interval drawn as a band makes "cannot tell
yet" obvious before any digits are read, which the word `THIN` never manages. The same
idea carries the inbox age bars and the watchlist cards.

Two rules the interface keeps:

- **Opening a repository costs nothing.** It reads the last probe out of the event log.
  Re-probing is a separate, explicit button, because it spends rate limit and a button is
  easier to click twice than a cron job is to fire twice.
- **The dashboard is never a second source of truth.** Hiding an inbox item writes the
  same record the digest reads, so the two cannot disagree about what you have already
  seen.

---

## Fresh and free, and why it needs its own fetch

The first three opportunity rules all answer the same question - has this been
abandoned - and the listing endpoint answers it alone. None of them answers the
question that actually gets asked on a free evening: is there something here nobody
has started yet.

That needs two facts `/issues` does not carry:

- **Is a pull request already open against it.** The listing has no link between an
  issue and a PR at all.
- **Has somebody said they are on it.** The listing gives a comment count, not authors,
  and on most projects a comment *is* the claim - GitHub has no claim outside assignment.

So enrichment is a separate command over candidates only, and what it writes stays close
to raw: linked pull requests, and each comment author, association and opening words.
Reading "claimed" or "validated" out of that is derivation, so a wrong rule can be
fixed and replayed rather than having been baked into the log.

**The digest still makes no network call.** Enrichment writes observations; the digest
reads them. That boundary is what keeps a digest reproducible from the log alone.

**An unenriched issue is never offered.** Unknown is not the same as free, and this rule
exists precisely to avoid a race that cannot be seen from the listing.

---

## The schedule is a floor, so there is a button

Scheduled workflows are delayed under load and may be dropped, which GitHub documents
and this repository measures: polls 1 to 6 hours apart, and a 14:30 digest committing
at 18:2x. For a log that costs nothing. For "I have twenty minutes free now" it is
useless.

So each thread header carries **Check now**, which the Worker turns into a
`workflow_dispatch` of `now.yml`: poll, enrich, send what is new. The Worker still
cannot write to GitHub - it queues a job, and the job is the same read-only path.

---

## Things that were wrong

Kept deliberately, because the reasoning is more useful than a clean history.

| What | Why it was wrong | What replaced it |
|---|---|---|
| `cold_merges >= 3` | An absolute count with no denominator; wrong in both directions | Wilson interval on a rate |
| Coverage gating the verdict | A proxy for "too small to conclude" | The interval, which measures it directly |
| Counting silence with no minimum age | Made fast trackers look negligent by construction | 72-hour threshold |
| Trusting `authorAssociation` for history | Describes the present, so old first merges vanish | Newness derived from author history |
| `refresh --revisit` | A flag existing to work around `refresh` doing the wrong thing by default | `refresh` refreshes everything |
| Probing on every detail-view open | Spent rate limit on a click, silently | Reads the cached probe; re-probing is a button |
| Request models inside `create_app()` | `from __future__ import annotations` made their hints unresolvable; every POST answered `422` | Hoisted to module scope |
| Per-request timeouts only | Bounded one request, not an operation that makes nine | A deadline over the whole operation |
| `bug` as evidence of triage | Issue templates apply it on filing, so it reports what the reporter clicked. Eight litellm issues qualified on it alone | Accepting labels are triage decisions (`confirmed`, `accepting prs`); a maintainer reply is the other route |
| Digest requests sent back to back, failing on the first 429 | Tripped Discord's per-channel bucket on the seventh item with 0.3s to wait. The record step then skipped on failure, so six delivered items went unrecorded and their thread id was lost | One second between requests, capped retries, a send deadline, partial sends recorded, and a record step that always runs |
