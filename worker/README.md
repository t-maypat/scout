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

It dispatches `digest.yml`, which polls, enriches and sends whatever is new — the same
workflow the daily schedule runs, so there is one code path rather than two.

It exists because a cron here is unreliable in two different ways, both measured: a
`*/15` schedule asked for ~1,050 runs and GitHub created **76**, and a once-daily
schedule was never dropped but fired **2.9 to 5.2 hours late, every day**. A dispatch
has no scheduler in the path — this API call creates the run — so the button is the
reliable trigger and the cron is the backstop.

Already-sent items are never repeated, so tapping it twice is harmless: you get what is
new since the last digest, which is usually what you wanted anyway.

The reply is ephemeral and immediate; the run takes a minute or two and posts into the
same day threads. As with every other button, the Worker only queues a job — the job runs
the same read-only code path as the cron.
