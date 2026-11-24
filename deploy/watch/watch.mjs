// Mothli's watcher: a Cloudflare Worker that emails the owner when the site is down, or when one of
// the server's scheduled jobs fails or goes quiet (runbook, "Alerts: how Mothli tells you something
// is wrong"). Every decision is a plain function below; the Worker around them only reads and
// writes the state and sends the email. No request body is ever logged.
//
// Settings (Worker variables or secrets; none is written here):
//   SITE_URL     the site, for example https://reader.example.org (no slash at the end)
//   MAIL_FROM    the sender, an address on the domain whose Email Routing is on
//   MAIL_TO      the owner's inbox, a verified destination address in Email Routing
//   WATCH_TOKEN  the secret the server sends with each report (MOTHLI_WATCH_TOKEN in deploy/.env)
//   JOBS_OFF     optional: jobs not watched, comma separated, for example "paddle-check"
// Bindings: STATE (one KV namespace), EMAIL (send_email).

const MINUTE = 60 * 1000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;
export const FAIL_MAIL_EVERY = 6 * HOUR;
export const SILENCE_MAIL_EVERY = DAY;
export const HEALTH_TIMEOUT_MS = 10 * 1000;
export const REASON_LIMIT = 300;

export const JOBS = {
  backup: {
    silentAfter: 26 * HOUR,
    what: "last night's backup",
    itself: "the nightly backup",
    look: "cd ~/mothli && tail -n 4 deploy/backups/backup.log",
    read: 'step 4.2, "Did last night\'s backup run?"',
  },
  purge: {
    silentAfter: 26 * HOUR,
    what: "today's erasing of deleted accounts",
    itself: "the daily erasing of deleted accounts",
    look: "cd ~/mothli && tail -n 20 deploy/backups/purge.log",
    read: 'step 4a, "Erasing deleted accounts" (and "Deleting an account while paying" if an ' +
      "account is waiting on Paddle)",
  },
  "paddle-check": {
    silentAfter: 3 * HOUR,
    what: "the hourly Paddle check",
    itself: "the hourly Paddle check",
    look: "cd ~/mothli && tail -n 5 deploy/backups/paddle-check.log",
    read: 'step 4c, "The regular check"',
  },
};

// ---- Plain words for times -----------------------------------------------------------------------

export function when(ms) {
  return new Date(ms).toISOString().slice(0, 16).replace("T", " ") + " UTC";
}

export function howLong(ms) {
  const minutes = Math.max(0, Math.round(ms / MINUTE));
  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
  if (minutes < 60) return plural(minutes, "minute");
  const hours = Math.floor(minutes / 60);
  if (hours < 48) {
    const rest = minutes % 60;
    return rest ? `${plural(hours, "hour")} ${plural(rest, "minute")}` : plural(hours, "hour");
  }
  return plural(Math.floor(hours / 24), "day");
}

const sinceLine = (start, now) => `${when(start)} (${howLong(now - start)} ago)`;

// ---- The token -----------------------------------------------------------------------------------

// Constant time whatever the lengths: both sides are hashed first, then every byte is compared.
export async function tokenMatches(given, expected) {
  if (typeof given !== "string" || typeof expected !== "string" || expected.length < 32) {
    return false;
  }
  const encode = (text) => crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  const [a, b] = (await Promise.all([encode(given), encode(expected)])).map((d) => new Uint8Array(d));
  let diff = 0;
  for (let i = 0; i < a.length; i += 1) diff |= a[i] ^ b[i];
  return diff === 0;
}

// ---- Reading a report ----------------------------------------------------------------------------

export function jobsOff(env) {
  return new Set(String(env.JOBS_OFF || "").split(",").map((s) => s.trim()).filter(Boolean));
}

// "/report/backup/fail" -> { job: "backup", outcome: "fail" }; anything else -> null.
export function parseReportPath(method, pathname) {
  if (method !== "POST") return null;
  const match = /^\/report\/([a-z-]+)\/(ok|fail)$/.exec(pathname);
  if (!match || !Object.hasOwn(JOBS, match[1])) return null;
  return { job: match[1], outcome: match[2] };
}

// One short line: printable characters only, spaces for everything else, at most 300 characters.
export function cleanReason(text) {
  return String(text || "")
    .slice(0, 4 * REASON_LIMIT)
    .replace(/[^\x20-\x7E]+/g, " ")
    .replace(/ {2,}/g, " ")
    .trim()
    .slice(0, REASON_LIMIT);
}

// ---- The decisions -------------------------------------------------------------------------------
// Each returns null (nothing changes, nothing is written) or { next, mail, unsent }: the state to
// save, the email to send (or null), and the state to save instead if that email cannot be sent,
// so that it is tried again (null: save nothing).

export function decideReport(job, old, outcome, reason, now) {
  const info = JOBS[job];
  const before = old || {};
  if (outcome === "ok") {
    const next = { lastOk: now, lastReport: now };
    let mail = null;
    if (before.failing) {
      mail = {
        subject: `Mothli: ${info.itself} works again`,
        text: `${capital(info.itself)} reported that it worked, at ${when(now)}.\n` +
          `It had been failing since ${sinceLine(before.failSince, now)}.\n\nNothing to do.`,
      };
    } else if (before.silenceMailedAt) {
      mail = {
        subject: `Mothli: ${info.itself} reported again`,
        text: `${capital(info.itself)} reported that it worked, at ${when(now)}, after being ` +
          `quiet since ${sinceLine(before.lastReport, now)}.\n\nNothing to do.`,
      };
    }
    return { next, mail, unsent: next };
  }
  const failSince = before.failing ? before.failSince : now;
  const mailNow = !before.failMailedAt || now - before.failMailedAt >= FAIL_MAIL_EVERY;
  const next = {
    lastOk: before.lastOk ?? null,
    lastReport: now,
    failing: true,
    failSince,
    failMailedAt: mailNow ? now : before.failMailedAt,
  };
  const mail = mailNow ? failMail(job, reason, failSince, now) : null;
  return { next, mail, unsent: { ...next, failMailedAt: before.failMailedAt ?? null } };
}

function failMail(job, reason, failSince, now) {
  const info = JOBS[job];
  const lines = [
    `${capital(info.what)} failed: the server reported it at ${when(now)}.`,
  ];
  if (failSince !== now) lines.push(`It has been failing since ${sinceLine(failSince, now)}.`);
  if (reason) lines.push(`What it said: ${reason}`);
  lines.push(
    "",
    "First, on the server, look at the last lines of its log:",
    `  ${info.look}`,
    "",
    `Then read the runbook (docs/runbooks/hosted-deployment.md), ${info.read}.`,
    "",
    "You will get this again at most every 6 hours while it keeps failing, and one email when " +
      "it works again.",
  );
  const subject = job === "purge" && /paddle/i.test(reason || "")
    ? "Mothli: a deleted account is waiting on Paddle"
    : `Mothli: ${info.what} failed`;
  return { subject, text: lines.join("\n") };
}

// A job is quiet when it has not reported at all (ok or fail) for its time; a failing job already
// emails on each failure. A job that has never said ok is not watched yet.
export function decideSilence(job, old, now) {
  const info = JOBS[job];
  if (!old || !old.lastOk) return null;
  if (now - old.lastReport < info.silentAfter) return null;
  if (old.silenceMailedAt && now - old.silenceMailedAt < SILENCE_MAIL_EVERY) return null;
  const mail = {
    subject: `Mothli: no word from ${info.itself} for ${howLong(now - old.lastReport)}`,
    text: [
      `${capital(info.itself)} has not reported since ${sinceLine(old.lastReport, now)}.`,
      "Either it did not run, or the server could not reach this watcher.",
      "",
      "First, on the server, check that its line is still in the crontab, then look at its log:",
      "  crontab -l",
      `  ${info.look}`,
      "",
      `Then read the runbook (docs/runbooks/hosted-deployment.md), ${info.read}, and the part ` +
        '"Alerts: how Mothli tells you something is wrong".',
      "",
      "You will get this once a day until it reports again.",
    ].join("\n"),
  };
  return { next: { ...old, silenceMailedAt: now }, mail, unsent: null };
}

// check: { ready: true } or { ready: false, problem: "it answered with code 503" }.
export function decideHealth(old, check, now, siteUrl) {
  const before = old || { everReady: false, fails: 0, since: null, mailed: false };
  if (check.ready) {
    const next = { everReady: true, fails: 0, since: null, mailed: false };
    if (sameState(before, next)) return null;
    const mail = before.mailed
      ? {
        subject: "Mothli: the site is back",
        text: `The site answered "ready" again at ${when(now)}.\n` +
          `It was not answering from ${sinceLine(before.since, now)}.\n\nNothing to do.`,
      }
      : null;
    return { next, mail, unsent: next };
  }
  if (!before.everReady) return null; // quiet until the site has answered ready once
  const next = {
    everReady: true,
    fails: before.fails + 1,
    since: before.since ?? now,
    mailed: before.mailed,
  };
  let mail = null;
  if (next.fails >= 2 && !before.mailed) {
    next.mailed = true;
    mail = {
      subject: "Mothli: the site is not answering",
      text: [
        `The site has not answered "ready" since ${sinceLine(next.since, now)}.`,
        `Last check: ${check.problem}.`,
        "",
        "First, from any computer, ask it yourself:",
        `  curl ${siteUrl}/api/health/ready`,
        "",
        "Then read the runbook (docs/runbooks/hosted-deployment.md), step 7, " +
          '"A reader says the site is down".',
        "",
        "You will get one more email when it is back.",
      ].join("\n"),
    };
  }
  return { next, mail, unsent: { ...next, mailed: before.mailed } };
}

// The answer of /api/health/ready -> { ready } or { ready: false, problem }.
export function judgeHealth(status, bodyText) {
  if (status !== 200) return { ready: false, problem: `it answered with code ${status}` };
  try {
    if (JSON.parse(bodyText).status === "ready") return { ready: true };
  } catch {
    // not JSON: falls through
  }
  return { ready: false, problem: 'it answered, but not "ready"' };
}

function sameState(a, b) {
  return JSON.stringify(a) === JSON.stringify(b);
}

function capital(text) {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

// ---- The Worker ----------------------------------------------------------------------------------

const notFound = () => new Response(null, { status: 404 });

async function readState(env, key) {
  return (await env.STATE.get(key, "json")) || null;
}

// Sends the email if there is one, then saves what the decision says; never throws for a lost email.
async function apply(env, key, decision) {
  if (!decision) return;
  let state = decision.next;
  if (decision.mail) {
    try {
      await env.EMAIL.send({
        from: env.MAIL_FROM,
        to: env.MAIL_TO,
        subject: decision.mail.subject,
        text: decision.mail.text,
      });
    } catch (error) {
      console.error(`watch: the email could not be sent (${error && error.code ? error.code : "error"})`);
      state = decision.unsent;
    }
  }
  if (state) await env.STATE.put(key, JSON.stringify(state));
}

async function checkSite(siteUrl) {
  try {
    const response = await fetch(`${siteUrl}/api/health/ready`, {
      redirect: "manual",
      signal: AbortSignal.timeout(HEALTH_TIMEOUT_MS),
    });
    return judgeHealth(response.status, await response.text());
  } catch {
    return { ready: false, problem: "it gave no answer within 10 seconds" };
  }
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const report = parseReportPath(request.method, url.pathname);
    if (!report) return notFound();
    const header = request.headers.get("Authorization") || "";
    const given = header.startsWith("Bearer ") ? header.slice(7) : "";
    if (!(await tokenMatches(given, env.WATCH_TOKEN))) return notFound();
    const reason = cleanReason(await request.text());
    if (!jobsOff(env).has(report.job)) {
      const key = `job:${report.job}`;
      const now = Date.now();
      await apply(env, key, decideReport(report.job, await readState(env, key), report.outcome, reason, now));
    }
    return new Response(null, { status: 204 });
  },

  async scheduled(controller, env) {
    const now = controller.scheduledTime;
    const off = jobsOff(env);
    for (const job of Object.keys(JOBS)) {
      if (off.has(job)) continue;
      const key = `job:${job}`;
      await apply(env, key, decideSilence(job, await readState(env, key), now));
    }
    const siteUrl = String(env.SITE_URL || "").replace(/\/+$/, "");
    if (siteUrl) {
      const check = await checkSite(siteUrl);
      await apply(env, "health", decideHealth(await readState(env, "health"), check, now, siteUrl));
    }
  },
};
