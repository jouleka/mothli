/** Hosted mode: where the reader stands with Mothli (ADR 0045 step 5, ADR 0046), as the app knows
 * it now.
 *
 * It starts from the session's `mothli` and changes, without a reload, when the server says so:
 * any Mothli route answering `402 mothli_plan` (a question, a stored conversation, the cast, the
 * notes, or a reply refused mid-stream), and the plan sheet saying the reader is on the plan.
 * `new` is a reader who has never had a plan or a gift; `ended` one whose plan or gift stopped.
 * Null where the server told nothing (local mode, no chat, the plan unreadable just then,
 * offline): the app then behaves exactly as before. Only the server decides; this is the app's
 * copy of its last word. */
import { useSyncExternalStore } from "react";

export type MothliStanding = {
  state: "new" | "paying" | "ended";
  why: "plan" | null;
};

/** The code every gated route answers a reader who does not pay with. */
export const MOTHLI_PLAN = "mothli_plan";

const STATES = new Set(["new", "paying", "ended"]);

let standing: MothliStanding | null = null;
let email: string | null = null;
const listeners = new Set<() => void>();
const tell = () => listeners.forEach((listener) => listener());

const whyOf = (value: unknown): MothliStanding["why"] => (value === "plan" ? "plan" : null);

/** The server's object, checked; anything else is "nothing told". */
export function standingOf(value: unknown): MothliStanding | null {
  if (!value || typeof value !== "object") return null;
  const { state, why } = value as Record<string, unknown>;
  if (typeof state !== "string" || !STATES.has(state)) return null;
  return { state: state as MothliStanding["state"], why: state === "ended" ? whyOf(why) : null };
}

/** Set from the session (and in tests). `readerEmail` is prefilled in the plan sheet's checkout. */
export function setMothliStanding(next: unknown, readerEmail?: string | null): void {
  standing = standingOf(next);
  if (readerEmail !== undefined) email = readerEmail;
  tell();
}

export function mothliStanding(): MothliStanding | null {
  return standing;
}

export function mothliEmail(): string | null {
  return email;
}

/** The server refused a Mothli route with `mothli_plan`: why `plan` is a reader whose plan or
 * gift stopped, anything else one who has never had either. */
export function noteMothliPlan(why: unknown): void {
  const next: MothliStanding = whyOf(why) === "plan" ? { state: "ended", why: "plan" } : { state: "new", why: null };
  if (standing?.state === next.state && standing.why === next.why) return;
  standing = next;
  tell();
}

/** The plan sheet says the reader is on the plan. */
export function noteMothliJoined(): void {
  standing = { state: "paying", why: null };
  tell();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function useMothliStanding(): MothliStanding | null {
  return useSyncExternalStore(subscribe, mothliStanding, mothliStanding);
}

/** Whether Mothli is closed to the reader (new or ended): the invitation stands in place of each
 * Mothli panel. Null (the server told nothing) and paying show Mothli. */
export function mothliClosed(): boolean {
  const state = standing?.state;
  return state === "new" || state === "ended";
}

export function useMothliClosed(): boolean {
  const state = useMothliStanding()?.state;
  return state === "new" || state === "ended";
}
