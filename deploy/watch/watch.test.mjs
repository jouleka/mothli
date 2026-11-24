// The watcher's decisions, run with Node's own test runner: `node --test deploy/watch/watch.test.mjs`.
import assert from "node:assert/strict";
import { test } from "node:test";

import watch, {
  cleanReason,
  decideHealth,
  decideReport,
  decideSilence,
  howLong,
  judgeHealth,
  parseReportPath,
  tokenMatches,
} from "./watch.mjs";

const MIN = 60 * 1000;
const HOUR = 60 * MIN;
const T0 = Date.UTC(2026, 10, 24, 3, 0);
const SITE = "https://site.example";
const TOKEN = "a".repeat(64);

// ---- The site ------------------------------------------------------------------------------------

function runHealth(checks) {
  let state = null;
  const mails = [];
  checks.forEach((check, i) => {
    const d = decideHealth(state, check, T0 + i * 5 * MIN, SITE);
    if (!d) return;
    if (d.mail) mails.push(d.mail);
    state = d.next;
  });
  return { state, mails };
}

const UP = { ready: true };
const DOWN = { ready: false, problem: "it answered with code 503" };

test("the site: quiet until it has answered ready once", () => {
  const { mails, state } = runHealth([DOWN, DOWN, DOWN]);
  assert.equal(mails.length, 0);
  assert.equal(state, null);
});

test("the site: one blip is not an email; two downs in a row are one email; back is one", () => {
  assert.equal(runHealth([UP, DOWN, UP]).mails.length, 0);
  const { mails } = runHealth([UP, DOWN, DOWN, DOWN, DOWN, DOWN, UP, UP]);
  assert.deepEqual(mails.map((m) => m.subject), [
    "Mothli: the site is not answering",
    "Mothli: the site is back",
  ]);
  assert.match(mails[0].text, /since 2026-11-24 03:05 UTC \(5 minutes ago\)/);
  assert.match(mails[0].text, /curl https:\/\/site\.example\/api\/health\/ready/);
  assert.match(mails[0].text, /code 503/);
  assert.match(mails[1].text, /from 2026-11-24 03:05 UTC \(25 minutes ago\)/);
});

test("the site: nothing is written while nothing changes", () => {
  const { state } = runHealth([UP]);
  assert.equal(decideHealth(state, UP, T0 + HOUR, SITE), null);
});

test("the site: a lost down email is tried again on the next check", () => {
  const { state } = runHealth([UP, DOWN]);
  const second = decideHealth(state, DOWN, T0 + 10 * MIN, SITE);
  assert.ok(second.mail);
  assert.equal(second.unsent.mailed, false);
  assert.ok(decideHealth(second.unsent, DOWN, T0 + 15 * MIN, SITE).mail);
});

test("the site: what counts as ready", () => {
  assert.deepEqual(judgeHealth(200, '{"status":"ready"}'), { ready: true });
  assert.equal(judgeHealth(200, '{"status":"not_ready"}').ready, false);
  assert.equal(judgeHealth(200, "<html>").ready, false);
  assert.equal(judgeHealth(503, '{"status":"ready"}').problem, "it answered with code 503");
});

// ---- Reports -------------------------------------------------------------------------------------

test("a failure emails at once, then at most every 6 hours, then once when it works again", () => {
  let state = decideReport("paddle-check", null, "ok", "", T0).next;
  const mails = [];
  for (let h = 1; h <= 8; h += 1) {
    const d = decideReport("paddle-check", state, "fail", "checked 3, changed 0, failed 1", T0 + h * HOUR);
    if (d.mail) mails.push(d.mail);
    state = d.next;
  }
  assert.equal(mails.length, 2); // hours 1 and 7
  assert.equal(mails[0].subject, "Mothli: the hourly Paddle check failed");
  assert.match(mails[0].text, /tail -n 5 deploy\/backups\/paddle-check\.log/);
  assert.match(mails[1].text, /failing since 2026-11-24 04:00 UTC \(6 hours ago\)/);
  const back = decideReport("paddle-check", state, "ok", "", T0 + 9 * HOUR);
  assert.equal(back.mail.subject, "Mothli: the hourly Paddle check works again");
  assert.equal(decideReport("paddle-check", back.next, "ok", "", T0 + 10 * HOUR).mail, null);
});

test("a failed backup says what the log said and the command to look", () => {
  const d = decideReport("backup", null, "fail", "BACKUP FAILED while uploading to R2: ...", T0);
  assert.equal(d.mail.subject, "Mothli: last night's backup failed");
  assert.match(d.mail.text, /What it said: BACKUP FAILED while uploading to R2/);
  assert.match(d.mail.text, /tail -n 4 deploy\/backups\/backup\.log/);
  assert.match(d.mail.text, /step 4\.2/);
  assert.equal(d.unsent.failMailedAt, null); // a lost email is tried with the next failure
});

test("an account held for Paddle is named in the subject", () => {
  const d = decideReport("purge", null, "fail", "an account is waiting on Paddle", T0);
  assert.equal(d.mail.subject, "Mothli: a deleted account is waiting on Paddle");
});

// ---- Silence -------------------------------------------------------------------------------------

test("a job that never said ok is never quiet", () => {
  assert.equal(decideSilence("backup", null, T0 + 100 * HOUR), null);
  const failedOnly = decideReport("backup", null, "fail", "", T0).next;
  assert.equal(decideSilence("backup", failedOnly, T0 + 100 * HOUR), null);
});

test("quiet jobs: 26 hours for backup and purge, 3 for the Paddle check, once a day", () => {
  const backup = decideReport("backup", null, "ok", "", T0).next;
  assert.equal(decideSilence("backup", backup, T0 + 25 * HOUR), null);
  const first = decideSilence("backup", backup, T0 + 26 * HOUR);
  assert.equal(first.mail.subject, "Mothli: no word from the nightly backup for 26 hours");
  assert.match(first.mail.text, /crontab -l/);
  assert.equal(decideSilence("backup", first.next, T0 + 49 * HOUR), null);
  assert.ok(decideSilence("backup", first.next, T0 + 50 * HOUR).mail);

  const purge = decideReport("purge", null, "ok", "", T0).next;
  assert.equal(decideSilence("purge", purge, T0 + 25 * HOUR), null);
  assert.ok(decideSilence("purge", purge, T0 + 26 * HOUR));

  const paddle = decideReport("paddle-check", null, "ok", "", T0).next;
  assert.equal(decideSilence("paddle-check", paddle, T0 + 2 * HOUR), null);
  assert.ok(decideSilence("paddle-check", paddle, T0 + 3 * HOUR));

  const again = decideReport("backup", first.next, "ok", "", T0 + 30 * HOUR);
  assert.equal(again.mail.subject, "Mothli: the nightly backup reported again");
  assert.equal(decideSilence("backup", again.next, T0 + 31 * HOUR), null);
});

// ---- Small parts ---------------------------------------------------------------------------------

test("the token: only the exact token, and never a short one", async () => {
  assert.equal(await tokenMatches(TOKEN, TOKEN), true);
  assert.equal(await tokenMatches(`${TOKEN}x`, TOKEN), false);
  assert.equal(await tokenMatches("", TOKEN), false);
  assert.equal(await tokenMatches("short", "short"), false);
  assert.equal(await tokenMatches("", undefined), false);
});

test("report paths, reasons and times", () => {
  assert.deepEqual(parseReportPath("POST", "/report/backup/fail"), { job: "backup", outcome: "fail" });
  assert.equal(parseReportPath("GET", "/report/backup/ok"), null);
  assert.equal(parseReportPath("POST", "/report/other/ok"), null);
  assert.equal(parseReportPath("POST", "/report/backup/maybe"), null);
  assert.equal(parseReportPath("POST", "/report/constructor/ok"), null);
  assert.equal(cleanReason("one\nline\u0000 é"), "one line");
  assert.equal(cleanReason("x".repeat(1000)).length, 300);
  assert.equal(howLong(5 * MIN), "5 minutes");
  assert.equal(howLong(HOUR + MIN), "1 hour 1 minute");
  assert.equal(howLong(3 * 24 * HOUR), "3 days");
});

// ---- The Worker itself, with stand-ins for KV and email --------------------------------------------

function fakeEnv(extra = {}) {
  const store = new Map();
  const sent = [];
  let writes = 0;
  return {
    store,
    sent,
    writes: () => writes,
    env: {
      WATCH_TOKEN: TOKEN,
      SITE_URL: SITE,
      MAIL_FROM: "watch@site.example",
      MAIL_TO: "owner@site.example",
      STATE: {
        get: async (key) => (store.has(key) ? JSON.parse(store.get(key)) : null),
        put: async (key, value) => { writes += 1; store.set(key, value); },
      },
      EMAIL: { send: async (message) => { sent.push(message); return { messageId: "1" }; } },
      ...extra,
    },
  };
}

const post = (path, token, body = "") => new Request(`https://watch.example${path}`, {
  method: "POST",
  headers: token ? { Authorization: `Bearer ${token}` } : {},
  body,
});

test("the Worker answers 404 with nothing to everything but a report with the token", async () => {
  const { env, sent, writes } = fakeEnv();
  for (const request of [
    new Request("https://watch.example/"),
    post("/report/backup/fail", null),
    post("/report/backup/fail", "b".repeat(64)),
    post("/report/nope/fail", TOKEN),
  ]) {
    const response = await watch.fetch(request, env);
    assert.equal(response.status, 404);
    assert.equal(await response.text(), "");
  }
  assert.equal(sent.length, 0);
  assert.equal(writes(), 0);
});

test("the Worker records a report, emails a failure, and stays quiet for a job switched off", async () => {
  const { env, sent, store } = fakeEnv({ JOBS_OFF: "paddle-check" });
  assert.equal((await watch.fetch(post("/report/backup/fail", TOKEN, "BACKUP FAILED while x"), env)).status, 204);
  assert.equal(sent.length, 1);
  assert.equal(sent[0].to, "owner@site.example");
  assert.equal(sent[0].from, "watch@site.example");
  assert.ok(store.has("job:backup"));
  assert.equal((await watch.fetch(post("/report/paddle-check/fail", TOKEN), env)).status, 204);
  assert.equal(sent.length, 1);
  assert.ok(!store.has("job:paddle-check"));
});

test("the Worker's cron run: down twice is one email, and a lost email does not stop the run", async (t) => {
  const { env, sent, writes } = fakeEnv();
  const answers = [[200, '{"status":"ready"}'], [503, "{}"], [503, "{}"], [503, "{}"]];
  t.mock.method(globalThis, "fetch", async () => {
    const [status, body] = answers.shift();
    return new Response(body, { status });
  });
  for (let i = 0; i < 4; i += 1) {
    await watch.scheduled({ scheduledTime: T0 + i * 5 * MIN }, env);
  }
  assert.equal(sent.length, 1);
  assert.equal(sent[0].subject, "Mothli: the site is not answering");
  assert.equal(writes(), 4); // ready once, then each changed down count

  const failing = fakeEnv();
  failing.env.EMAIL.send = async () => { throw Object.assign(new Error("no"), { code: "E_X" }); };
  t.mock.method(console, "error", () => {});
  const report = await watch.fetch(post("/report/backup/fail", TOKEN), failing.env);
  assert.equal(report.status, 204);
  assert.equal(JSON.parse(failing.store.get("job:backup")).failMailedAt, null);
});
