import type { AskCitation } from "../api";
export type SavedCompanionReply = { id: string; body: string; question: string; citations: AskCitation[]; asOfChapter: number; createdAt?: string };
// Keep the legacy namespace so a brand change cannot orphan saved replies.
const PREFIX = "rc:pip-keeps:";
const key = (book: string, owner: string, pass: number | string) => `${PREFIX}${JSON.stringify([owner, book, String(pass)])}`;
export function readSavedReplies(book: string, owner: string, pass: number | string = 0): SavedCompanionReply[] {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(key(book, owner, pass)) ?? "[]");
    return Array.isArray(value) ? value.filter((item): item is SavedCompanionReply => item && typeof item.id === "string" && typeof item.body === "string" && typeof item.question === "string" && Number.isInteger(item.asOfChapter) && item.asOfChapter >= 0 && Array.isArray(item.citations) && item.citations.every((citation: AskCitation) => citation && Number.isInteger(citation.id) && Number.isInteger(citation.ordinal) && citation.ordinal >= 0 && [citation.chapter_key, citation.href, citation.title, citation.excerpt].every(part => typeof part === "string"))) : [];
  } catch { return []; }
}
/** Storage failures propagate so the caller can keep the reply and offer a retry. */
export function writeSavedReplies(book: string, owner: string, pass: number | string, replies: SavedCompanionReply[]): void {
  localStorage.setItem(key(book, owner, pass), JSON.stringify(replies));
}
/** The reading passes of this book that have replies kept under them, oldest first. Replies are
 * filed under the pass they were kept in; one from an earlier pass is not lost by a reset. */
export function keptPasses(book: string, owner: string): string[] {
  const passes = new Set<string>();
  try {
    for (let index = 0; index < localStorage.length; index++) {
      const name = localStorage.key(index);
      if (!name?.startsWith(PREFIX)) continue;
      try {
        const parts: unknown = JSON.parse(name.slice(PREFIX.length));
        if (Array.isArray(parts) && parts[0] === owner && parts[1] === book && typeof parts[2] === "string") passes.add(parts[2]);
      } catch { /* not one of ours */ }
    }
  } catch { /* storage unavailable */ }
  return [...passes].sort((left, right) => Number(left) - Number(right) || left.localeCompare(right));
}
/** Whether a kept reply may be shown now, by the read map (share read of each chapter, by 0-based
 * chapter index). The rule the chat panel follows: a reply stays only while the text it was written
 * from still counts as read. Here that is the chapter it was written at and every chapter it cites:
 * each must be at least part read. A place that is not a chapter of the book proper (the opening
 * pages) is not in the map and decides nothing. */
export function keptReplyShown(reply: SavedCompanionReply, readShare: Record<number, number>): boolean {
  const chapters = [reply.asOfChapter, ...reply.citations.map((citation) => citation.ordinal)];
  return chapters.every((atom) => atom < 1 || !(atom - 1 in readShare) || readShare[atom - 1] > 0);
}
