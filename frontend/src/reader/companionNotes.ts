import type { AskCitation } from "../api";
export type SavedCompanionReply = { id: string; body: string; question: string; citations: AskCitation[]; asOfChapter: number; createdAt?: string };
const key = (book: string, owner: string, pass: number | string) => `rc:pip-keeps:${JSON.stringify([owner, book, String(pass)])}`;
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
