import { describe, expect, test, vi } from "vitest";
import { atomAtRange, pageAnchor, ReadFrontierReporter } from "./readFrontier";

describe("exact page source anchors", () => {
  test("tracks a passed page separately from the visible-page confirmation", () => {
    document.body.innerHTML = "<p>Mara hid the key. Later comes the unread secret.</p>";
    const node = document.querySelector("p")!.firstChild!;
    const range = document.createRange();
    range.setStart(node, 5); range.setEnd(node, 17);
    const passed = pageAnchor(range, 0, "epubcfi(/6/2!/4/2/1:5)", 0, "start")!;
    const visible = pageAnchor(range, 0, passed.cfi, 0, "end")!;
    expect(passed.prefix).toBe("Mara");
    expect(visible.prefix).toBe("Mara hid the key.");
    expect(visible.quote.startsWith(visible.prefix)).toBe(true);
  });
  test("preserves Unicode prefix positions without surrogate splitting", () => {
    document.body.innerHTML = "<p>Мара 🙂 спрятала ключ. 未読の秘密</p>";
    const node = document.querySelector("p")!.firstChild!;
    const range = document.createRange(); range.setStart(node, 0); range.setEnd(node, 20);
    const value = pageAnchor(range, 0, "epubcfi(/6/2)", 3, "end")!;
    expect(value.position_epoch).toBe(3);
    expect(value.quote.startsWith(value.prefix)).toBe(true);
    expect(value.prefix).not.toContain("未読");
  });
  test("resolves the actual anchored chapter within one spine document", () => {
    document.body.innerHTML = '<h2 id="one">One</h2><p>Earlier.</p><h2 id="two">Two</h2><p>Current page.</p>';
    const range = document.createRange(); range.selectNodeContents(document.querySelectorAll("p")[1]);
    const atoms = [{href:"book.xhtml#one"}, {href:"book.xhtml#two"}] as Parameters<typeof atomAtRange>[0];
    expect(atomAtRange(atoms, "book.xhtml", range, "start")).toBe(1);
    document.querySelector("h2")!.id = "two";
    expect(atomAtRange(atoms, "book.xhtml", range, "start")).toBe(null);
  });
  test("retains the furthest queued page through a quick backward turn", async () => {
    vi.useFakeTimers();
    const put = vi.fn(async () => null);
    const reporter = new ReadFrontierReporter(put);
    const value = {atom:1, quote:"source", prefix:"sou", position_epoch:0};
    reporter.schedule({...value,cfi:"epubcfi(/6/2!/4/2/1:100)"});
    reporter.schedule({...value,cfi:"epubcfi(/6/2!/4/2/1:20)"});
    await reporter.flush();
    expect(put).toHaveBeenCalledWith(expect.objectContaining({cfi:"epubcfi(/6/2!/4/2/1:100)"}));
    reporter.dispose(); vi.useRealTimers();
  });
});
