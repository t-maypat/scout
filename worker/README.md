# scout interaction receiver

The only non-Python part of scout, and the only part with a public URL.

Discord will not talk to a cron job. When someone taps a button it makes a signed POST
and wants an answer inside three seconds, so something has to be listening. That is all
this does: verify the Ed25519 signature, decode the button id, and fire a
`workflow_dispatch` at the scout repository. The Python in Actions does the real work.

It holds no state deliberately. There is no second store to keep consistent with the
event log, and the endpoint cannot post a comment or open a pull request even if someone
forges their way past the signature check.

## Deploy

```bash
npm install -g wrangler
cd worker
wrangler secret put DISCORD_PUBLIC_KEY   # Discord app -> General Information
wrangler secret put GITHUB_TOKEN         # fine-grained PAT, Actions read+write, scout repo only
wrangler secret put GITHUB_REPO          # owner/scout
wrangler deploy
```

Then paste the deployed URL into the Discord application's **Interactions Endpoint URL**.
Discord immediately sends a PING and rejects the URL unless it gets a signed PONG back
and a 401 for a bad signature, so a successful save means verification works.

## Button ids

`<action>:<owner>/<repo>:<number>` — for example `claim:pola-rs/polars:8821`.

`snooze` and `dismiss` are answered inline and picked up by the next poll. `claim` and
`dispatch` queue an Actions run. Nothing here writes to GitHub on your behalf.

## Check now

Thread headers carry one control button, whose id has no item number:

`poll:<owner>/<repo>` — for example `poll:BerriAI/litellm`.

It dispatches `now.yml`, which polls, enriches and sends whatever is new. It exists
because scheduled runs are late: measured on this repository, polls land 1 to 6 hours
apart and a 14:30 UTC digest committed at 18:2x. GitHub documents that schedules are
delayed under load and that queued jobs may be dropped, so the schedule is a floor and
this button is how you ask for an answer now.

The reply is ephemeral and immediate; the run takes a minute or two and posts into the
same day threads. As with every other button, the Worker only queues a job — the job runs
the same read-only code path as the cron.
