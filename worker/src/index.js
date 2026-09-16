/**
 * Discord interaction receiver.
 *
 * The only part of scout that needs a public HTTPS endpoint, and the only part that is
 * not Python. Discord will not talk to a cron job: when someone taps a button it makes a
 * signed POST and expects an answer within three seconds. Nothing else here holds state,
 * reads GitHub, or makes a decision - it verifies the signature and forwards the intent
 * to a workflow_dispatch, where the Python does the real work.
 *
 * Two rules this file exists to keep:
 *
 *   1. Every request is verified before it is read as anything but bytes. Discord's
 *      endpoint is public, so an unverified body is attacker-controlled input.
 *   2. Nothing irreversible happens here. A tap queues a job; the job still has to do
 *      its own checking. This endpoint cannot post a comment or open a pull request.
 *
 * Secrets (wrangler secret put NAME):
 *   DISCORD_PUBLIC_KEY  - from the Discord application's General Information page
 *   GITHUB_TOKEN        - a PAT with `actions:write` on your scout repository only
 *   GITHUB_REPO         - "owner/scout"
 */

const PING = 1;
const APPLICATION_COMMAND = 2;
const MESSAGE_COMPONENT = 3;

const PONG = 1;
const CHANNEL_MESSAGE_WITH_SOURCE = 4;
const EPHEMERAL = 1 << 6;

// custom_id format: "<action>:<owner>/<repo>:<number>", e.g. "claim:pola-rs/polars:8821"
const CUSTOM_ID = /^(claim|dispatch|snooze|dismiss):([\w.-]+\/[\w.-]+):(\d+)$/;
// A control button belongs to a repository rather than one item: "poll:owner/repo".
const CONTROL_ID = /^(poll):([\w.-]+\/[\w.-]+)$/;
// Scheduled runs are late by hours; this is the way to ask for a check right now. It
// polls, enriches and sends whatever is new, all of it the same read-only code path.
const CHECK_NOW_WORKFLOW = "now.yml";

const hexToBytes = (hex) =>
  Uint8Array.from(hex.match(/.{1,2}/g) ?? [], (byte) => parseInt(byte, 16));

/**
 * Ed25519 over (timestamp + raw body), exactly as Discord signs it. The raw text must be
 * used - re-serialising the parsed JSON changes the bytes and the signature will fail.
 */
async function verify(request, rawBody, publicKey) {
  const signature = request.headers.get("x-signature-ed25519");
  const timestamp = request.headers.get("x-signature-timestamp");
  if (!signature || !timestamp) return false;

  try {
    const key = await crypto.subtle.importKey(
      "raw",
      hexToBytes(publicKey),
      { name: "Ed25519", namedCurve: "Ed25519" },
      false,
      ["verify"],
    );
    return await crypto.subtle.verify(
      { name: "Ed25519" },
      key,
      hexToBytes(signature),
      new TextEncoder().encode(timestamp + rawBody),
    );
  } catch {
    return false;
  }
}

const reply = (content) =>
  Response.json({
    type: CHANNEL_MESSAGE_WITH_SOURCE,
    data: { content, flags: EPHEMERAL },
  });

/** Queue a workflow in GitHub Actions. Returns the HTTP status, never throws. */
async function runWorkflow(env, workflow, inputs) {
  const response = await fetch(
    `https://api.github.com/repos/${env.GITHUB_REPO}/actions/workflows/${workflow}/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        Accept: "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": "scout-worker",
      },
      body: JSON.stringify({ ref: env.GITHUB_REF || "main", inputs }),
    },
  );
  return response.status;
}

/** Queue the work for one item. Returns a human-readable outcome, never throws. */
async function dispatch(env, action, repo, number, actor) {
  if (!env.GITHUB_TOKEN || !env.GITHUB_REPO) {
    return "Not wired up yet — the worker has no GitHub token.";
  }
  const status = await runWorkflow(env, "dispatch.yml", {
    action,
    repo,
    number: String(number),
    actor,
  });
  if (status === 204) {
    return `Queued **${action}** on \`${repo}#${number}\`. Nothing has been posted to GitHub — the job will report back here.`;
  }
  return `GitHub refused the dispatch (${status}). Check the worker's token scopes.`;
}

/** Poll, enrich and send whatever is new, now rather than whenever the cron lands. */
async function checkNow(env, repo, actor) {
  if (!env.GITHUB_TOKEN || !env.GITHUB_REPO) {
    return "Not wired up yet — the worker has no GitHub token.";
  }
  const status = await runWorkflow(env, CHECK_NOW_WORKFLOW, { repo, actor });
  if (status === 204) {
    return `Checking \`${repo}\` now. Anything new lands in today's threads in a minute or two; nothing is written to GitHub.`;
  }
  if (status === 404) {
    return `GitHub has no \`${CHECK_NOW_WORKFLOW}\` on the default branch yet (404).`;
  }
  return `GitHub refused the dispatch (${status}). Check the worker's token scopes.`;
}

export default {
  async fetch(request, env) {
    if (request.method !== "POST") {
      return new Response("scout interaction receiver", { status: 200 });
    }

    const rawBody = await request.text();
    if (!(await verify(request, rawBody, env.DISCORD_PUBLIC_KEY))) {
      // Discord requires exactly 401 here, and checks for it during endpoint setup.
      return new Response("bad signature", { status: 401 });
    }

    let interaction;
    try {
      interaction = JSON.parse(rawBody);
    } catch {
      return new Response("bad body", { status: 400 });
    }

    if (interaction.type === PING) {
      return Response.json({ type: PONG });
    }

    if (interaction.type !== MESSAGE_COMPONENT && interaction.type !== APPLICATION_COMMAND) {
      return Response.json({ type: PONG });
    }

    const customId = interaction.data?.custom_id ?? "";
    const actor =
      interaction.member?.user?.username ?? interaction.user?.username ?? "unknown";

    const control = CONTROL_ID.exec(customId);
    if (control) {
      return reply(await checkNow(env, control[2], actor));
    }

    const match = CUSTOM_ID.exec(customId);
    if (!match) {
      return reply("I do not recognise that button.");
    }

    const [, action, repo, number] = match;

    if (action === "snooze" || action === "dismiss") {
      // Recorded by the next poll rather than acted on here, so the receiver stays
      // stateless and there is nothing to keep consistent between two stores.
      return reply(`Noted — \`${repo}#${number}\` ${action}d.`);
    }

    return reply(await dispatch(env, action, repo, number, actor));
  },
};
