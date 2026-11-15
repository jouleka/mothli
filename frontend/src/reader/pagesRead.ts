/** Which pages count as read. Mothli knows a page once the reader has stayed on it long enough
 * to have read it, or is asking about it while it is on screen. Pages flicked past on the way
 * somewhere else are never reported. */
import type { ReadAnchor } from "../api";

export const WORDS_PER_MINUTE = 300; // ordinary reading
const LEAST_MS = 1500;

/** A quarter of ordinary reading time for the page's words: faster than anyone reads, slower
 * than anyone flicks. */
export function longEnough(words: number, shownMs: number): boolean {
  return shownMs >= Math.max(LEAST_MS, ((words / WORDS_PER_MINUTE) * 60_000) / 4);
}

type Page = { start: ReadAnchor; end: ReadAnchor; words: number; shownAt: number };

export class PagesRead {
  private page: Page | null = null;
  constructor(
    private send: (start: ReadAnchor, end: ReadAnchor) => Promise<unknown>,
    private now: () => number = () => Date.now(),
  ) {}

  /** A new page is on screen. The one before it counts if the reader stayed on it. */
  show(start: ReadAnchor | null, end: ReadAnchor | null, words: number) {
    this.leave();
    this.page = start && end ? { start, end, words, shownAt: this.now() } : null;
  }

  /** The page on screen is going away: a page turn, a jump, or the book closing. */
  leave() {
    const page = this.page;
    this.page = null;
    if (page && longEnough(page.words, this.now() - page.shownAt))
      void this.send(page.start, page.end).catch(() => undefined);
  }

  /** The reader is asking about what is on screen, so it counts now. */
  async confirmCurrent() {
    if (this.page) await this.send(this.page.start, this.page.end);
  }
}
