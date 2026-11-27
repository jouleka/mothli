/** Hosted mode: the question a reader who does not pay asked anyway, kept for them (ADR 0046,
 * "Keep their question").
 *
 * One question waits per book, and one on the home screen, on this device only (never on the
 * server), for the reader signed in here. It stays until the reader removes it, edits it, or it is
 * taken to be sent: `takeKeptQuestion` hands it out once and forgets it in the same step, so a
 * waiting question is sent at most once. A copy is held in memory too, so it still works for this
 * visit where storage is unavailable. */
import { useSyncExternalStore } from "react";
import { activeOfflineOwner } from "../pwa/offline";

const PREFIX = "mothli.keptQuestion:";
/** As much as the boxes take. */
const LONGEST = 500;

/** Where a question waits: a book, or the home screen. */
export type KeptScope = { book: string } | "home";

const memory = new Map<string, string | null>();
const listeners = new Set<() => void>();
const tell = () => listeners.forEach((listener) => listener());

function keyOf(scope: KeptScope): string {
  const where = scope === "home" ? "home" : `book:${encodeURIComponent(scope.book)}`;
  return `${PREFIX}${encodeURIComponent(activeOfflineOwner())}:${where}`;
}

function read(key: string): string | null {
  let stored: unknown;
  try {
    stored = JSON.parse(localStorage.getItem(key) ?? "null");
  } catch {
    // No storage here: what this visit kept.
    return memory.get(key) ?? null;
  }
  const question = (stored as { question?: unknown } | null)?.question;
  return typeof question === "string" && question.trim().length >= 2 ? question.slice(0, LONGEST) : null;
}

function write(key: string, question: string | null) {
  memory.set(key, question);
  try {
    if (question) localStorage.setItem(key, JSON.stringify({ question }));
    else localStorage.removeItem(key);
  } catch {
    /* Kept for this visit only. */
  }
  tell();
}

/** The question waiting here, if any. */
export function keptQuestion(scope: KeptScope): string | null {
  return read(keyOf(scope));
}

/** Keep the reader's question here, in place of any that was waiting. */
export function keepQuestion(scope: KeptScope, question: string): void {
  const words = question.trim().slice(0, LONGEST);
  if (words.length >= 2) write(keyOf(scope), words);
}

/** The reader removed it, or took it back into the box to edit. */
export function dropKeptQuestion(scope: KeptScope): void {
  const key = keyOf(scope);
  if (read(key) !== null) write(key, null);
}

/** The waiting question, handed out once to be sent: it is forgotten here in the same step, so a
 * second caller (a second panel, a second effect run) gets nothing. */
export function takeKeptQuestion(scope: KeptScope): string | null {
  const key = keyOf(scope);
  const question = read(key);
  if (question !== null) write(key, null);
  return question;
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function useKeptQuestion(scope: KeptScope): string | null {
  const get = () => keptQuestion(scope);
  return useSyncExternalStore(subscribe, get, get);
}
