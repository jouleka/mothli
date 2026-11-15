/** Which pages count as read. Mothli knows a page once the reader has stayed on it long enough
 * to have read it, or is asking about it while it is on screen.
 *
 * A page left quickly for the very next page was skimmed: turned on the way somewhere. It is
 * reported as skimmed, which the server keeps apart from what was read, so that Mothli can ask
 * once the reader settles. A page left quickly any other way (a link, the contents list, turning
 * back, closing the book) was not skimmed through: nothing is reported for it. */
import type { ReadAnchor } from "../api";

export const WORDS_PER_MINUTE = 300; // ordinary reading
const LEAST_MS = 1500;

/** A quarter of ordinary reading time for the page's words: faster than anyone reads, slower
 * than anyone flicks. */
export function longEnough(words: number, shownMs: number): boolean {
  return shownMs >= Math.max(LEAST_MS, ((words / WORDS_PER_MINUTE) * 60_000) / 4);
}

/** Where a page sits: the book file it is in, and the page of that file (as the paginator counts
 * them, with a blank page at each end of a paginated file). */
export type PageAt = { section: number; page: number; pages: number };

/** Whether ``b`` is the page that follows ``a``: the next page of the same file, or the start of
 * the next file when ``a`` was the last page of text in its own. */
export function nextPage(a: PageAt, b: PageAt): boolean {
  if (b.section === a.section) return b.page === a.page + 1;
  return b.section === a.section + 1 && a.page >= a.pages - 2 && b.page <= 1;
}

type Page = {
  start: ReadAnchor; end: ReadAnchor; words: number; shownAt: number; at: PageAt | null;
  counted?: boolean; // already reported as read, because the reader asked about it
};

export class PagesRead {
  private page: Page | null = null;
  private left: Page | null = null; // left too quickly to be read; skimmed if the next page follows it
  constructor(
    private send: (start: ReadAnchor, end: ReadAnchor, skimmed: boolean) => Promise<unknown>,
    private now: () => number = () => Date.now(),
  ) {}

  /** A new page is on screen. The one before it counts if the reader stayed on it, and was
   * skimmed if they left it quickly for this one. */
  show(start: ReadAnchor | null, end: ReadAnchor | null, words: number, at: PageAt | null = null) {
    this.leave();
    const left = this.left;
    this.left = null;
    if (left?.at && at && nextPage(left.at, at)) this.report(left, true);
    this.page = start && end ? { start, end, words, shownAt: this.now(), at } : null;
  }

  /** The page on screen is going away: a page turn, a jump, or the book closing. */
  leave() {
    const page = this.page;
    if (!page) return;
    this.page = null;
    this.left = null;
    if (page.counted) return;
    if (longEnough(page.words, this.now() - page.shownAt)) this.report(page, false);
    else this.left = page;
  }

  /** The reader is asking about what is on screen, so it counts now. */
  async confirmCurrent() {
    const page = this.page;
    if (!page) return;
    await this.send(page.start, page.end, false);
    page.counted = true;
  }

  private report(page: Page, skimmed: boolean) {
    void this.send(page.start, page.end, skimmed).catch(() => undefined);
  }
}
