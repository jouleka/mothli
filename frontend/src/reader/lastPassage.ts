/** "Where you stopped": the first lines of the page the reader was last on, kept on this device so
 * the library can show them without asking the server or a model for anything. It is text the
 * reader has already had in front of them, so it can never run ahead of their place. */

const key = (owner: string, bookId: string) => `mothli.lastPassage:${owner}:${bookId}`;
const LIMIT = 420;

export type LastPassage = { text: string; at: number };

/** Keep whole sentences where possible, whole words otherwise. */
export function trimPassage(raw: string, limit = LIMIT): string {
  const clean = raw.replace(/\s+/g, " ").trim();
  if (clean.length <= limit) return clean;
  const cut = clean.slice(0, limit);
  const stop = Math.max(cut.lastIndexOf(". "), cut.lastIndexOf("! "), cut.lastIndexOf("? "), cut.lastIndexOf(".” "), cut.lastIndexOf("?” "), cut.lastIndexOf("!” "));
  if (stop > limit * 0.5) return cut.slice(0, cut.indexOf(" ", stop)).trim();
  return `${cut.replace(/\s+\S*$/, "").trimEnd()}…`;
}

export function saveLastPassage(owner: string, bookId: string, raw: string, now = Date.now()) {
  const text = trimPassage(raw);
  if (text.length < 40) return; // a title page or an illustration is not "where you stopped"
  try {
    localStorage.setItem(key(owner, bookId), JSON.stringify({ text, at: now } satisfies LastPassage));
  } catch {
    /* storage unavailable: the library simply shows its usual note */
  }
}

export function readLastPassage(owner: string, bookId: string): LastPassage | null {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(key(owner, bookId)) ?? "null");
    if (!value || typeof value !== "object") return null;
    const { text, at } = value as Record<string, unknown>;
    return typeof text === "string" && text && typeof at === "number" ? { text, at } : null;
  } catch {
    return null;
  }
}

export function forgetLastPassage(owner: string, bookId: string) {
  try {
    localStorage.removeItem(key(owner, bookId));
  } catch {
    /* nothing to forget */
  }
}

/** "today", "yesterday", "3 days ago", "2 weeks ago": how long since the reader was last here. */
export function sinceLabel(at: number, now = Date.now()): string {
  const days = Math.floor((now - at) / 86_400_000);
  if (days < 1) return "today";
  if (days < 2) return "yesterday";
  if (days < 14) return `${days} days ago`;
  if (days < 60) return `${Math.round(days / 7)} weeks ago`;
  return `${Math.round(days / 30)} months ago`;
}

/** "about 6 minutes left in this chapter", from the reader engine's own estimate. */
export function minutesLeftLabel(minutes: number | null | undefined, unit = "chapter"): string | null {
  if (minutes == null || !Number.isFinite(minutes) || minutes < 0) return null;
  if (minutes < 1) return `Under a minute left in this ${unit}`;
  const rounded = Math.round(minutes);
  if (rounded >= 90) return `About ${Math.round(rounded / 60)} hours left in this ${unit}`;
  return `About ${rounded} ${rounded === 1 ? "minute" : "minutes"} left in this ${unit}`;
}
