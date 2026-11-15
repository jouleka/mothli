import { describe, expect, test, vi } from "vitest";
import { atomAtRange, NO_CHAPTER, pageAnchor, ReadFrontierReporter } from "./readFrontier";

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
  test("text ahead of a file's first chapter anchor belongs to the previous chapter", () => {
    document.body.innerHTML = '<p>End of six.</p><h2 id="seven">Seven</h2><p>Start of seven.</p><h2 id="eight">Eight</h2>';
    const range = document.createRange(); range.selectNodeContents(document.querySelectorAll("p")[0]);
    const atoms = [{href:"a.xhtml#six"}, {href:"b.xhtml#seven"}, {href:"b.xhtml#eight"}] as Parameters<typeof atomAtRange>[0];
    expect(atomAtRange(atoms, "b.xhtml", range, "start")).toBe(0);
    range.selectNodeContents(document.querySelectorAll("p")[1]);
    expect(atomAtRange(atoms, "b.xhtml", range, "start")).toBe(1);
  });
  // The break: Project Gutenberg's header, contents list and preface, which share a file with
  // chapter one, were placed in chapter one. The server holds them in no chapter, so every such
  // page was reported and refused (422), and the reader's place followed them under "Chapter I".
  test("Gutenberg's pages ahead of the book's first chapter are in no chapter", () => {
    document.body.innerHTML = '<header><h2>The Project Gutenberg eBook of Split</h2><p>This eBook is for the use of anyone anywhere.</p>'
      + '<p>*** START OF THE PROJECT GUTENBERG EBOOK SPLIT ***</p></header><p class="toc">CONTENTS</p><p>A preface.</p>'
      + '<h2 id="c1">Chapter I.</h2><p>Mara arrived.</p><h2 id="c2">Chapter II.</h2><p>Jonas waited.</p>';
    const atoms = [{href:"f0.html#c1"}, {href:"f0.html#c2"}, {href:"f1.html#c3"}] as Parameters<typeof atomAtRange>[0];
    const range = document.createRange();
    for (const front of [document.querySelector("header p")!, document.querySelector(".toc")!, document.querySelectorAll("p")[3]]) {
      range.selectNodeContents(front);
      expect(atomAtRange(atoms, "f0.html", range, "start")).toBe(NO_CHAPTER);
      expect(atomAtRange(atoms, "f0.html", range, "end")).toBe(NO_CHAPTER);
    }
    range.selectNodeContents(document.querySelectorAll("p")[4]);
    expect(atomAtRange(atoms, "f0.html", range, "start")).toBe(0);
    range.selectNodeContents(document.querySelectorAll("p")[5]);
    expect(atomAtRange(atoms, "f0.html", range, "start")).toBe(1);
  });
  // The break the other way: prose ahead of the first chapter heading in any other book is the
  // opening of chapter one on the server, and must stay reportable.
  test("prose ahead of the first chapter anchor opens chapter one when the file is not Gutenberg front matter", () => {
    document.body.innerHTML = '<p>A prologue before any heading.</p><h2 id="c1">Chapter I.</h2><p>One.</p><h2 id="c2">Chapter II.</h2>';
    const atoms = [{href:"a.html#c1"}, {href:"a.html#c2"}] as Parameters<typeof atomAtRange>[0];
    const range = document.createRange(); range.selectNodeContents(document.querySelector("p")!);
    expect(atomAtRange(atoms, "a.html", range, "start")).toBe(0);
  });
  // The break: a file with one chapter anchor gave all its text to that chapter, the text ahead
  // of the anchor included.
  test("text ahead of a file's only chapter anchor is not that chapter's", () => {
    document.body.innerHTML = '<p>The Project Gutenberg eBook of Split</p><h2 id="c1">Chapter I.</h2><p>One.</p>';
    const range = document.createRange(); range.selectNodeContents(document.querySelector("p")!);
    const first = [{href:"f0.html#c1"}, {href:"f1.html#c2"}] as Parameters<typeof atomAtRange>[0];
    expect(atomAtRange(first, "f0.html", range, "start")).toBe(NO_CHAPTER);
    const later = [{href:"a.html#c0"}, {href:"f0.html#c1"}] as Parameters<typeof atomAtRange>[0];
    expect(atomAtRange(later, "f0.html", range, "start")).toBe(0);
    range.selectNodeContents(document.querySelectorAll("p")[1]);
    expect(atomAtRange(later, "f0.html", range, "start")).toBe(1);
    // A chapter that is its whole file needs no anchor.
    expect(atomAtRange([{href:"f0.html"}] as Parameters<typeof atomAtRange>[0], "f0.html", range, "start")).toBe(0);
  });
  // The break: on a file that holds no chapter (a cover, the licence) the lookup threw.
  test("a file that holds no chapter is not placed", () => {
    document.body.innerHTML = "<p>*** END OF THE PROJECT GUTENBERG EBOOK SPLIT ***</p>";
    const range = document.createRange(); range.selectNodeContents(document.querySelector("p")!);
    const atoms = [{href:"f0.html#c1"}, {href:"f0.html#c2"}] as Parameters<typeof atomAtRange>[0];
    expect(atomAtRange(atoms, "licence.html", range, "start")).toBe(null);
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
  test("concurrent flush callers await both the active and trailing page confirmations", async () => {
    vi.useFakeTimers();
    const releases: Array<() => void> = [];
    const put = vi.fn(() => new Promise<null>((resolve) => releases.push(() => resolve(null))));
    const reporter = new ReadFrontierReporter(put);
    const value = {atom:1, quote:"source", prefix:"sou", position_epoch:0};
    reporter.schedule({...value,cfi:"epubcfi(/6/2!/4/2/1:100)"});
    let firstDone = false;
    const first = reporter.flush().then(() => { firstDone = true; });
    reporter.schedule({...value,cfi:"epubcfi(/6/2!/4/2/1:200)"});
    let secondDone = false;
    const second = reporter.flush().then(() => { secondDone = true; });
    releases[0]();
    await vi.advanceTimersByTimeAsync(0);
    expect(put).toHaveBeenCalledTimes(2);
    expect(firstDone).toBe(false);
    expect(secondDone).toBe(false);
    releases[1]();
    await Promise.all([first,second]);
    expect(firstDone && secondDone).toBe(true);
    reporter.dispose(); vi.useRealTimers();
  });
  test("a rejected active write stops all waiters and preserves the latest page for retry", async () => {
    vi.useFakeTimers();
    let reject!: (reason: Error) => void;
    const put = vi.fn().mockImplementationOnce(() => new Promise<null>((_resolve,fail) => { reject = fail; }))
      .mockResolvedValue(null);
    const reporter = new ReadFrontierReporter(put);
    const value = {atom:1, quote:"source", prefix:"sou", position_epoch:0};
    reporter.schedule({...value,cfi:"epubcfi(/6/2!/4/2/1:100)"});
    const first = reporter.flush();
    reporter.schedule({...value,cfi:"epubcfi(/6/2!/4/2/1:200)"});
    const settled = Promise.allSettled([first,reporter.flush()]);
    reject(new Error("offline"));
    expect((await settled).map((result) => result.status)).toEqual(["rejected","rejected"]);
    expect(put).toHaveBeenCalledTimes(1);
    await reporter.flush();
    expect(put).toHaveBeenLastCalledWith(expect.objectContaining({cfi:"epubcfi(/6/2!/4/2/1:200)"}));
    reporter.dispose(); vi.useRealTimers();
  });
  test("disposing an in-flight confirmation never submits its queued successor", async () => {
    vi.useFakeTimers();
    let release!: () => void;
    const put = vi.fn(() => new Promise<null>((resolve) => { release = () => resolve(null); }));
    const reporter = new ReadFrontierReporter(put);
    const value = {atom:1, quote:"source", prefix:"sou", position_epoch:0};
    reporter.schedule({...value,cfi:"epubcfi(/6/2!/4/2/1:100)"});
    const active = reporter.flush();
    reporter.schedule({...value,cfi:"epubcfi(/6/2!/4/2/1:200)"});
    reporter.dispose();
    release();
    await active;
    await vi.advanceTimersByTimeAsync(500);
    expect(put).toHaveBeenCalledTimes(1);
    vi.useRealTimers();
  });
});
