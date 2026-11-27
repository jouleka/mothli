/** Mothli's note at the end of a book (hosted mode, a reader who pays): one ordinary reply in the
 * book's own conversation, asked with a fixed line from the app as if the reader had said it.
 *
 * Nothing here writes or checks a word Mothli says: the line goes the way any question goes
 * (`api.askBookStream`, the server's `stream_read_prefix`), and every sentence that comes back has
 * already been checked by the server. The whole book is read by then, so the usual "up to your
 * place" is the whole book.
 *
 * Once per book per reader: the server's stored conversation is looked at first (a free read), so
 * a line already asked on any device is not asked again, and this device remembers it too, so a
 * reload or finishing again sends nothing. At the reader's day's rest the server answers without
 * calling anything, and the note is simply not left. */
import { api, type Atom, type ReadFrontier } from "../api";
import { activeOfflineOwner } from "../pwa/offline";
import { chapterCount, chaptersFinished } from "./chapterPlace";

/** What the reader "says" when they finish: Mothli's reply is the note. */
export const BOOK_END_LINE = "I just finished the book! What did you make of the ending?";

const key = (bookId: string) => `mothli.bookEndNote:${activeOfflineOwner()}:${bookId}`;

export function bookEndNoteLeft(bookId: string): boolean {
  try {
    return localStorage.getItem(key(bookId)) === "yes";
  } catch {
    return false;
  }
}

function rememberBookEndNote(bookId: string): void {
  try {
    localStorage.setItem(key(bookId), "yes");
  } catch {
    /* Without storage the server's conversation still says it was asked. */
  }
}

/** Whether moving the chapter counter from `previous` to `bookmark` finished the book: the last of
 * its chapters, as the footer counts them, is now done. Opening a finished book is not finishing. */
export function finishedTheBook(atoms: Atom[], previous: number | null, bookmark: number): boolean {
  const total = chapterCount(atoms);
  if (previous == null || total < 1) return false;
  return chaptersFinished(atoms, previous) < total && chaptersFinished(atoms, bookmark) >= total;
}

/** Ask for the note. Resolves with the checked sentences to show, or null when there is nothing
 * to show: already asked, the day's rest, Mothli busy, offline, or no reply that could be shown. */
export async function leaveBookEndNote(
  bookId: string,
  prepareReadContext: () => Promise<ReadFrontier | null>,
  isCurrent: () => boolean,
): Promise<string | null> {
  if (bookEndNoteLeft(bookId) || !api.askBookStream) return null;
  // Claimed before anything is asked: a reload while the reply streams never asks twice.
  rememberBookEndNote(bookId);
  try {
    const stored = await api.bookConversation?.(bookId);
    if (stored?.turns?.some((turn) => turn.role === "user" && turn.content.trim() === BOOK_END_LINE))
      return null;
    const point = await prepareReadContext();
    if (!point || !isCurrent()) return null;
    const sentences: string[] = [];
    const answer = await api.askBookStream(bookId, BOOK_END_LINE, point.available_chapter, point,
      (sentence) => sentences.push(sentence));
    if (!answer || answer.insufficient_evidence || !isCurrent()) return null;
    // The same check the chat panel makes of any reply (AskTheBook's `send`).
    if (answer.as_of_chapter > point.available_chapter ||
        answer.citations.some((citation) => citation.ordinal > point.available_chapter) ||
        answer.read_frontier?.id !== point.id)
      return null;
    const text = sentences.join(" ").trim();
    return text || null;
  } catch {
    // The day's rest (or the app's), Mothli still busy, the plan stopped, offline: nothing shown,
    // and the note is not asked again.
    return null;
  }
}
