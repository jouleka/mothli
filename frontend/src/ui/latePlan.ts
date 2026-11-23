/** Hosted mode: a payment Paddle completed that reaches Mothli only after the plan sheet stopped
 * waiting for it (ADR 0045, the plan walk of 23 November 2026). The sheet waits a minute; then
 * this keeps asking the server quietly, for a bounded time, wherever the reader is by then:
 *
 * - every `LATE_ASK_EVERY_MS` for `LATE_REPEAT_FOR_MS` after the sheet gave up, and
 * - once each time the tab is shown again, for `LATE_WHEN_SHOWN_FOR_MS` after that.
 *
 * The moment the server says the reader is on the plan, the sheet if it is still open says so
 * (it claims the news), otherwise where the reader stands becomes paying at once and every panel
 * opens up without a reload. After the last bound nothing more is asked: the next page load reads
 * the plan from the session, and the invitation's own sheet reads it whenever it is opened. */
import { api, type Plan } from "../api";
import { noteMothliJoined } from "./mothliStanding";

export const LATE_ASK_EVERY_MS = 15_000;
export const LATE_REPEAT_FOR_MS = 15 * 60_000;
export const LATE_WHEN_SHOWN_FOR_MS = 24 * 60 * 60_000;

/** Something on screen that says the news itself (the plan sheet). True when it did. */
type Claim = (plan: Plan) => boolean;
const claims = new Set<Claim>();

let watch: { started: number; timer: number; asking: boolean } | null = null;

/** Start watching for a late payment (again from now if already watching). */
export function watchForLatePlan(): void {
  stopWatchingForLatePlan();
  watch = { started: Date.now(), timer: 0, asking: false };
  document.addEventListener("visibilitychange", onShown);
  later();
}

/** Nothing more is asked (tests, and once the plan has arrived). */
export function stopWatchingForLatePlan(): void {
  if (watch) window.clearTimeout(watch.timer);
  watch = null;
  document.removeEventListener("visibilitychange", onShown);
}

/** Whether the watch is still on (tests). */
export function watchingForLatePlan(): boolean {
  return watch !== null;
}

/** While `claim` is registered, it is told first; returns the unregistering function. */
export function claimLatePlan(claim: Claim): () => void {
  claims.add(claim);
  return () => {
    claims.delete(claim);
  };
}

function later(): void {
  if (!watch || Date.now() - watch.started >= LATE_REPEAT_FOR_MS) return;
  window.clearTimeout(watch.timer);
  watch.timer = window.setTimeout(() => void ask(true), LATE_ASK_EVERY_MS);
}

function onShown(): void {
  if (!watch || document.visibilityState !== "visible") return;
  if (Date.now() - watch.started >= LATE_REPEAT_FOR_MS + LATE_WHEN_SHOWN_FOR_MS) {
    stopWatchingForLatePlan();
    return;
  }
  void ask(false);
}

async function ask(repeat: boolean): Promise<void> {
  const current = watch;
  if (!current) return;
  if (current.asking) {
    if (repeat) later();
    return;
  }
  current.asking = true;
  let found: Plan | null = null;
  try {
    const plan = await api.plan();
    if (plan.subscribed) found = plan;
  } catch {
    // A passing failure: asked again at the next turn.
  }
  if (watch !== current) return;
  current.asking = false;
  if (found) {
    stopWatchingForLatePlan();
    const plan = found;
    if (![...claims].some((claim) => claim(plan))) noteMothliJoined();
    return;
  }
  if (repeat) later();
}
