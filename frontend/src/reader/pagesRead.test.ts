import { describe, expect, test, vi } from "vitest";
import type { ReadAnchor } from "../api";
import { longEnough, nextPage, PagesRead, type PageAt } from "./pagesRead";

const at = (quote: string): ReadAnchor => ({ atom: 1, quote, prefix: "", cfi: "epubcfi(/6/2)", position_epoch: 0 });
const page = (section: number, n: number, pages = 10): PageAt => ({ section, page: n, pages });

describe("which pages count as read", () => {
  test("a page counts after a quarter of ordinary reading time, never under a second and a half", () => {
    // 300 words is a minute of ordinary reading: fifteen seconds is long enough, flicking past is not.
    expect(longEnough(300, 14_000)).toBe(false);
    expect(longEnough(300, 15_000)).toBe(true);
    expect(longEnough(10, 1_400)).toBe(false);
    expect(longEnough(10, 1_500)).toBe(true);
  });

  test("leaving a page reports it as read only if the reader stayed on it", () => {
    let now = 0;
    const send = vi.fn().mockResolvedValue(null);
    const pages = new PagesRead(send, () => now);
    pages.show(at("one"), at("one end"), 300, page(1, 1));
    now += 20_000;
    pages.show(at("two"), at("two end"), 300, page(1, 2));      // turned the page after reading it
    now += 400;
    pages.show(at("three"), at("three end"), 300, page(1, 3));  // turned quickly past page two
    now += 300;
    pages.show(at("four"), at("four end"), 300, page(1, 4));    // and page three
    const read = send.mock.calls.filter(([, , skimmed]) => !skimmed).map(([start]) => start.quote);
    expect(read).toEqual(["one"]);
  });

  test("asking about the page on screen counts it at once, and a page with no anchors is never reported", async () => {
    const send = vi.fn().mockResolvedValue(null);
    const pages = new PagesRead(send, () => 0);
    pages.show(at("one"), at("one end"), 300, page(1, 1));
    await pages.confirmCurrent();
    expect(send).toHaveBeenCalledTimes(1);
    pages.show(null, at("x"), 300, page(1, 2));
    await pages.confirmCurrent();
    pages.leave();
    expect(send).toHaveBeenCalledTimes(1);
  });

  test("a report that fails does not break reading", async () => {
    let now = 0;
    const pages = new PagesRead(vi.fn().mockRejectedValue(new Error("offline")), () => now);
    pages.show(at("one"), at("one end"), 10, page(1, 1));
    now += 5_000;
    expect(() => pages.leave()).not.toThrow();
    now += 100;
    pages.show(at("two"), at("two end"), 10, page(1, 2));
    expect(() => pages.show(at("three"), at("three end"), 10, page(1, 3))).not.toThrow();
    await Promise.resolve();
  });
});

describe("skimmed pages", () => {
  test("pages turned quickly one after another are reported as skimmed, not read", () => {
    let now = 0;
    const send = vi.fn().mockResolvedValue(null);
    const pages = new PagesRead(send, () => now);
    pages.show(at("one"), at("one end"), 300, page(1, 1, 4));
    now += 400;
    pages.leave();                                               // the relocate event comes first
    pages.show(at("two"), at("two end"), 300, page(1, 2, 4));    // the last page of text in the file
    now += 400;
    pages.show(at("three"), at("three end"), 300, page(2, 1));   // over into the next chapter
    now += 400;
    pages.show(at("four"), at("four end"), 300, page(2, 2));
    expect(send.mock.calls.map(([start, , skimmed]) => [start.quote, skimmed]))
      .toEqual([["one", true], ["two", true], ["three", true]]);
  });

  test("a jump records nothing: not the page left, not the page landed on", () => {
    let now = 0;
    const send = vi.fn().mockResolvedValue(null);
    const pages = new PagesRead(send, () => now);
    pages.show(at("one"), at("one end"), 300, page(1, 1));
    now += 400;
    pages.show(at("far"), at("far end"), 300, page(7, 4));       // a link or the contents list
    now += 400;
    pages.show(at("back"), at("back end"), 300, page(1, 5));     // and back again
    now += 400;
    pages.leave();                                               // the book closes
    expect(send).not.toHaveBeenCalled();
  });

  test("turning back is not skimming", () => {
    let now = 0;
    const send = vi.fn().mockResolvedValue(null);
    const pages = new PagesRead(send, () => now);
    pages.show(at("five"), at("five end"), 300, page(1, 5));
    now += 400;
    pages.show(at("four"), at("four end"), 300, page(1, 4));
    expect(send).not.toHaveBeenCalled();
  });

  test("the next page is the one after in the same file, or the start of the next file from the end of this one", () => {
    expect(nextPage(page(1, 3), page(1, 4))).toBe(true);
    expect(nextPage(page(1, 3), page(1, 5))).toBe(false);
    // Paginated files carry a blank page at each end, so the last page of text is pages - 2.
    expect(nextPage(page(1, 8, 10), page(2, 1))).toBe(true);
    expect(nextPage(page(1, 5, 10), page(2, 1))).toBe(false);   // left mid-file for the next one
    expect(nextPage(page(1, 8, 10), page(2, 4))).toBe(false);
    expect(nextPage(page(1, 8, 10), page(3, 1))).toBe(false);
  });
});
