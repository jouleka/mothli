import { describe, expect, test, vi } from "vitest";
import type { ReadAnchor } from "../api";
import { longEnough, PagesRead } from "./pagesRead";

const at = (quote: string): ReadAnchor => ({ atom: 1, quote, prefix: "", cfi: "epubcfi(/6/2)", position_epoch: 0 });

describe("which pages count as read", () => {
  test("a page counts after a quarter of ordinary reading time, never under a second and a half", () => {
    // 300 words is a minute of ordinary reading: fifteen seconds is long enough, flicking past is not.
    expect(longEnough(300, 14_000)).toBe(false);
    expect(longEnough(300, 15_000)).toBe(true);
    expect(longEnough(10, 1_400)).toBe(false);
    expect(longEnough(10, 1_500)).toBe(true);
  });

  test("leaving a page reports it only if the reader stayed on it; flicked-past pages are never reported", () => {
    let now = 0;
    const send = vi.fn().mockResolvedValue(null);
    const pages = new PagesRead(send, () => now);
    pages.show(at("one"), at("one end"), 300);
    now += 20_000;
    pages.show(at("two"), at("two end"), 300);      // turned the page after reading it
    now += 400;
    pages.show(at("three"), at("three end"), 300);  // flicked past page two
    now += 300;
    pages.show(at("four"), at("four end"), 300);    // and page three
    expect(send.mock.calls.map(([start]) => start.quote)).toEqual(["one"]);
  });

  test("asking about the page on screen counts it at once, and a page with no anchors is never reported", async () => {
    const send = vi.fn().mockResolvedValue(null);
    const pages = new PagesRead(send, () => 0);
    pages.show(at("one"), at("one end"), 300);
    await pages.confirmCurrent();
    expect(send).toHaveBeenCalledTimes(1);
    pages.show(null, at("x"), 300);
    await pages.confirmCurrent();
    pages.leave();
    expect(send).toHaveBeenCalledTimes(1);
  });

  test("a report that fails does not break reading", async () => {
    let now = 0;
    const pages = new PagesRead(vi.fn().mockRejectedValue(new Error("offline")), () => now);
    pages.show(at("one"), at("one end"), 10);
    now += 5_000;
    expect(() => pages.leave()).not.toThrow();
    await Promise.resolve();
  });
});
