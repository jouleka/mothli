/** Hosted mode: where the reader stands with Mothli (ADR 0045 step 5), as the app knows it now.
 *
 * It starts from the session's `mothli` and changes, without a reload, when the server says so:
 * any Mothli route answering `402 mothli_plan` (a question, a stored conversation, the cast, the
 * notes, or a reply refused mid-stream), the start route's answer, a first reply (a new reader's
 * free weeks have begun), and the plan sheet saying the reader is on the plan. Null where the
 * server told nothing (local mode, no chat, the plan unreadable just then, offline): the app then
 * behaves exactly as before. Only the server decides; this is the app's copy of its last word. */
import { useSyncExternalStore } from "react";

export type MothliStanding = {
  state: "new" | "free" | "paying" | "ended";
  why: "time" | "ceiling" | "plan" | null;
  ending_note: boolean;
};

/** The code every gated route answers an ended reader with. */
export const MOTHLI_PLAN = "mothli_plan";

const STATES = new Set(["new", "free", "paying", "ended"]);
const WHYS = new Set(["time", "ceiling", "plan"]);
// The gentle note's "Got it", remembered for each reader on this device: another reader signed in
// here later still sees their own note.
const NOTE_SEEN_KEY = "mothli.ending-note-seen";

let standing: MothliStanding | null = null;
let email: string | null = null;
let reader: string | null = null;
// Read again whenever the session says who the reader is (setMothliStanding).
let noteSeen = false;
const listeners = new Set<() => void>();
const tell = () => listeners.forEach((listener) => listener());

const noteSeenKey = () => (reader ? `${NOTE_SEEN_KEY}:${reader}` : null);

function readNoteSeen(): boolean {
  const key = noteSeenKey();
  if (!key) return false;
  try {
    return localStorage.getItem(key) === "yes";
  } catch {
    return false;
  }
}

function whyOf(value: unknown): MothliStanding["why"] {
  return typeof value === "string" && WHYS.has(value) ? (value as MothliStanding["why"]) : null;
}

/** The server's object, checked; anything else is "nothing told". */
export function standingOf(value: unknown): MothliStanding | null {
  if (!value || typeof value !== "object") return null;
  const { state, why, ending_note } = value as Record<string, unknown>;
  if (typeof state !== "string" || !STATES.has(state)) return null;
  return { state: state as MothliStanding["state"], why: whyOf(why), ending_note: ending_note === true };
}

/** Set from the session (and in tests). `readerEmail` is prefilled in the plan sheet's checkout;
 * `readerId` is whose "Got it" on the gentle note is remembered. */
export function setMothliStanding(next: unknown, readerEmail?: string | null, readerId?: string | null): void {
  standing = standingOf(next);
  if (readerEmail !== undefined) email = readerEmail;
  if (readerId !== undefined) reader = readerId;
  noteSeen = readNoteSeen();
  tell();
}

export function mothliStanding(): MothliStanding | null {
  return standing;
}

export function mothliEmail(): string | null {
  return email;
}

/** The server refused a Mothli route with `mothli_plan`: the reader's free weeks or plan ended. */
export function noteMothliPlan(why: unknown): void {
  const next = whyOf(why) ?? (standing?.state === "ended" ? standing.why : null);
  if (standing?.state === "ended" && standing.why === next) return;
  standing = { state: "ended", why: next, ending_note: false };
  tell();
}

/** A reply arrived for a new reader: their question started the free weeks on the server. */
export function noteMothliUsed(): void {
  if (standing?.state !== "new") return;
  standing = { state: "free", why: null, ending_note: false };
  tell();
}

/** The plan sheet says the reader is on the plan. */
export function noteMothliJoined(): void {
  standing = { state: "paying", why: null, ending_note: false };
  tell();
}

/** The gentle note's "Got it", remembered for this reader on this device. */
export function endingNoteSeen(): boolean {
  return noteSeen;
}

export function dismissEndingNote(): void {
  noteSeen = true;
  const key = noteSeenKey();
  try {
    if (key) localStorage.setItem(key, "yes");
  } catch {
    /* Remembered for this visit only. */
  }
  tell();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Re-renders on every change: the standing, and whether the note was dismissed. */
export function useMothliStanding(): MothliStanding | null {
  useSyncExternalStore(subscribe, endingNoteSeen, endingNoteSeen);
  return useSyncExternalStore(subscribe, mothliStanding, mothliStanding);
}

/** Only an ended standing ever shows the invitation; null and every other state show Mothli. */
export function useMothliEnded(): boolean {
  return useMothliStanding()?.state === "ended";
}
