import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  manifest: vi.fn(),
  books: vi.fn(),
  position: vi.fn(),
  readFrontier: vi.fn(async (..._args: unknown[]) => null),
  askBook: vi.fn(),
  readerPreferences: vi.fn(),
  readerMarks: vi.fn(),
  putPosition: vi.fn(),
  putReaderPreferences: vi.fn(),
  epubUrl: vi.fn(() => "/api/books/b/epub"),
  readerMarksExportUrl: vi.fn(() => "/api/books/b/marks/export"),
  makeBook: vi.fn(),
  destroyBook: vi.fn(),
  open: vi.fn(),
  init: vi.fn(),
  goTo: vi.fn(),
  close: vi.fn(),
  search: vi.fn(),
  clearSearch: vi.fn(),
  onSearch: null as null | ((query: string) => Promise<Array<{ cfi: string }>>),
  onMarkRead: null as null | undefined | ((atom: number, read: boolean) => void),
}));

vi.mock("../api", () => ({
  api: mocks,
  ApiError: class ApiError extends Error {
    constructor(
      message: string,
      readonly status: number,
    ) {
      super(message);
    }
  },
}));
vi.mock("../vendor/foliate-js/view.js", () => ({ makeBook: mocks.makeBook }));
vi.mock("./ReaderTTS", () => ({ ReaderTTS: () => null }));
vi.mock("./BookNavigation", () => ({
  ReaderNavigation: ({ onSearch, onMarkRead }: { onSearch: typeof mocks.onSearch; onMarkRead?: typeof mocks.onMarkRead }) => {
    mocks.onSearch = onSearch;
    mocks.onMarkRead = onMarkRead;
    return null;
  },
}));

import { ReaderPage } from "./ReaderPage";
import { ApiError } from "../api";
import { activeOfflineOwner, BOOK_GONE_EVENT, BOOK_REMOVED_KEY, pendingPosition } from "../pwa/offline";
import { readSavedReplies, writeSavedReplies } from "./companionNotes";
import { setMothliStanding } from "../ui/mothliStanding";

class TestFoliateView extends HTMLElement {
  renderer = Object.assign(document.createElement("div"), {
    pages: 2,
    page: 1,
    setStyles: vi.fn(),
  });
  history = Object.assign(new EventTarget(), {
    canGoBack: false,
    canGoForward: false,
  });
  book = {
    sections: [{ id: "c1.html" }, { id: "c2.html" }, { id: "c3.html" }],
  };
  open = mocks.open;
  init = mocks.init;
  goTo = mocks.goTo;
  addAnnotation = vi.fn(async () => {});
  close = mocks.close;
  search = mocks.search;
  clearSearch = mocks.clearSearch;
  getTOCItemOf = async (cfi: string) => ({
    href: `c${Number(cfi.split(":")[0]) + 1}.html`,
  });
  resolveNavigation = (cfi: string) => ({ index: Number(cfi.split(":")[0]) });
}
customElements.define("foliate-view", TestFoliateView);

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.readFrontier.mockReset().mockResolvedValue(null);
  localStorage.clear();
  const sections = [{ id: "c1.html" }, { id: "c2.html" }, { id: "c3.html" }];
  mocks.manifest.mockResolvedValue({
    book_id: "b",
    mode: "file-driven",
    book_type: "novel",
    atoms: sections.map((section, index) => ({
      ordinal: index + 1,
      href: section.id,
      title: `Chapter ${index + 1}`,
      char_len: 100,
    })),
  });
  mocks.books.mockResolvedValue([{ book_id: "b", title: "A test book" }]);
  mocks.position.mockResolvedValue({
    bookmark: 1,
    cfi: "0:resume",
    position_epoch: 0,
  });
  mocks.readerPreferences.mockResolvedValue({});
  mocks.readerMarks.mockResolvedValue({ marks: [] });
  mocks.makeBook.mockResolvedValue({
    sections,
    toc: sections.map((section) => ({ href: section.id })),
    destroy: mocks.destroyBook,
  });
  mocks.open.mockResolvedValue(undefined);
  mocks.init.mockResolvedValue(undefined);
  mocks.goTo.mockResolvedValue(undefined);
  mocks.search.mockImplementation(async function* () {
    yield { cfi: "0:read", excerpt: { pre: "", match: "answer", post: "" } };
    yield { cfi: "2:future", excerpt: { pre: "", match: "answer", post: "" } };
  });
  mocks.onSearch = null;
  mocks.onMarkRead = null;
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(new Blob(["epub"]), { status: 200 })),
  );
});
afterEach(() => vi.unstubAllGlobals());

describe("reader startup and search boundaries", () => {
  test("an immediate conversation confirms the jump's position before its page source", async () => {
    const gate = deferred();
    let positionCommitted = false;
    mocks.putPosition.mockImplementationOnce(async () => {
      await gate.promise;
      positionCommitted = true;
      return {bookmark:2,current_chapter:3,chapter_progress:0,position_epoch:0,cfi:"jump"};
    });
    mocks.readFrontier.mockImplementation(async (...args: unknown[]) => {
      if (args[1]) expect(positionCommitted).toBe(true);
      return null;
    });
    mocks.askBook.mockResolvedValue({as_of_chapter:2,insufficient_evidence:true,
      claims:[],citations:[],cost:null});
    const {unmount} = render(<ReaderPage bookId="b" onBack={() => {}} />);
    await waitFor(() => expect(mocks.init).toHaveBeenCalled());
    const doc = document.implementation.createHTMLDocument("chapter");
    doc.body.innerHTML = "<p>Visible chapter three sentence.</p>";
    const range = doc.createRange();
    range.setStart(doc.querySelector("p")!.firstChild!,0);
    range.setEnd(doc.querySelector("p")!.firstChild!,10);
    act(() => document.querySelector("foliate-view")!.dispatchEvent(new CustomEvent("relocate", {
      detail:{cfi:"epubcfi(/6/6!/4/2/1:0)",section:{current:2},range},
    })));
    fireEvent.click(screen.getByRole("button",{name:"Talk to Mothli"}));
    fireEvent.change(screen.getByLabelText("Message Mothli"),{target:{value:"Catch me up"}});
    fireEvent.submit(screen.getByLabelText("Message Mothli").closest("form")!);
    await waitFor(() => expect(mocks.putPosition).toHaveBeenCalled());
    expect(mocks.readFrontier.mock.calls.filter((args) => args[1])).toHaveLength(0);
    expect(mocks.askBook).not.toHaveBeenCalled();
    await act(async () => { gate.resolve(); await gate.promise; });
    await waitFor(() => expect(mocks.askBook).toHaveBeenCalled());
    expect(mocks.readFrontier.mock.calls.some((args) => args[1])).toBe(true);
    unmount();
  });
  // Catches (M4 of the plan walk, 23 November 2026): the same book open on another device moves
  // its place on the server, so this page's report comes back "not applied, newer progress
  // kept". That answer was taken as an unconfirmed position: every question then failed with "I
  // couldn’t get a reply. Try again?" before any request, Retry the same, until a reload.
  test("a place kept from another device does not stop the question, nor the next one", async () => {
    mocks.putPosition.mockResolvedValue({ bookmark: 2, current_chapter: 3, chapter_progress: 0, position_epoch: 0,
      cfi: "elsewhere", position_version: 7, applied: false, conflict: "stale_behind" });
    mocks.askBook.mockResolvedValue({ as_of_chapter: 1, insufficient_evidence: false,
      claims: [{ text: "Oren trims the lantern.", citation_ids: [] }], citations: [], cost: null });
    const { unmount } = render(<ReaderPage bookId="b" onBack={() => {}} />);
    await waitFor(() => expect(mocks.init).toHaveBeenCalled());
    act(() => document.querySelector("foliate-view")!.dispatchEvent(new CustomEvent("relocate", {
      detail: { cfi: "0:start", section: { current: 0 } },
    })));
    fireEvent.click(screen.getByRole("button", { name: "Talk to Mothli" }));
    fireEvent.change(screen.getByLabelText("Message Mothli"), { target: { value: "Who is Oren?" } });
    fireEvent.submit(screen.getByLabelText("Message Mothli").closest("form")!);
    await waitFor(() => expect(mocks.askBook).toHaveBeenCalledTimes(1));
    expect(mocks.putPosition).toHaveBeenCalledTimes(1);
    expect(await screen.findByText("Oren trims the lantern.")).toBeTruthy();
    expect(screen.queryByText("I couldn’t get a reply. Try again?")).toBeNull();
    expect(screen.getByText("Newer progress kept")).toBeTruthy();
    // And the next question too, with nothing reloaded.
    fireEvent.change(screen.getByLabelText("Message Mothli"), { target: { value: "And the harbour?" } });
    fireEvent.submit(screen.getByLabelText("Message Mothli").closest("form")!);
    await waitFor(() => expect(mocks.askBook).toHaveBeenCalledTimes(2));
    expect(screen.queryByText("I couldn’t get a reply. Try again?")).toBeNull();
    unmount();
  });

  // Catches: a place the server says is ahead of this device's copy (its version moved on): the
  // report was dropped and every question failed until a reload. The page takes the server's
  // place and sends its report again, once, by itself.
  test("a version the server refuses is refreshed by itself and the report sent again", async () => {
    mocks.putPosition
      .mockRejectedValueOnce(new ApiError("reading position version is ahead of the server; reload before continuing", 409))
      .mockResolvedValue({ bookmark: 1, current_chapter: 2, chapter_progress: 0, position_epoch: 0, cfi: "0:start",
        position_version: 4, applied: true });
    mocks.askBook.mockResolvedValue({ as_of_chapter: 1, insufficient_evidence: false,
      claims: [{ text: "Oren trims the lantern.", citation_ids: [] }], citations: [], cost: null });
    const { unmount } = render(<ReaderPage bookId="b" onBack={() => {}} />);
    await waitFor(() => expect(mocks.init).toHaveBeenCalled());
    mocks.position.mockResolvedValue({ bookmark: 1, cfi: "0:resume", position_epoch: 0, position_version: 3 });
    act(() => document.querySelector("foliate-view")!.dispatchEvent(new CustomEvent("relocate", {
      detail: { cfi: "0:start", section: { current: 0 } },
    })));
    fireEvent.click(screen.getByRole("button", { name: "Talk to Mothli" }));
    fireEvent.change(screen.getByLabelText("Message Mothli"), { target: { value: "Who is Oren?" } });
    fireEvent.submit(screen.getByLabelText("Message Mothli").closest("form")!);
    expect(await screen.findByText("Oren trims the lantern.")).toBeTruthy();
    expect(mocks.putPosition).toHaveBeenCalledTimes(2);
    expect(mocks.putPosition.mock.calls[1][5]).toBe(3);   // sent again on the server's version
    expect(screen.queryByText("I couldn’t get a reply. Try again?")).toBeNull();
    unmount();
  });

  // Catches: "I haven't read this" in Contents leaving the chat's earlier turns on screen. The
  // reading pass does not change, so only ReaderPage knows: it must take the conversation off
  // the screen at once and have the panel ask the server again once the change is saved.
  test("marking a chapter not read hides the conversation and asks the server for it again", async () => {
    const extra = mocks as Record<string, unknown>;
    const turn = (role: string, content: string) =>
      ({ role, content, atom: 1, reaction: false, created_at: "2020-01-01T10:00:00+00:00" });
    const bookConversation = vi.fn()
      .mockResolvedValueOnce({ turns: [turn("user", "Who is Mitya?"), turn("assistant", "Fyodor’s eldest son.")] })
      .mockResolvedValue({ turns: [] });
    let saveUnread!: () => void;
    const readChapters = vi.fn(() => new Promise<void>((resolve) => { saveUnread = resolve; }));
    extra.bookConversation = bookConversation;
    extra.readChapters = readChapters;
    extra.readMap = vi.fn().mockResolvedValue({ chapters: [{ atom: 1, read: 1 }] });
    try {
      const { unmount } = render(<ReaderPage bookId="b" onBack={() => {}} />);
      await waitFor(() => expect(mocks.init).toHaveBeenCalled());
      await waitFor(() => expect(mocks.onMarkRead).toBeTruthy());
      fireEvent.click(screen.getByRole("button", { name: "Talk to Mothli" }));
      await screen.findByText("Fyodor’s eldest son.");
      act(() => mocks.onMarkRead!(0, false));
      // Gone before the server has even taken the change, and not asked for until it has.
      expect(screen.queryByText("Fyodor’s eldest son.")).toBeNull();
      expect(readChapters).toHaveBeenCalledWith("b", [1], false);
      expect(bookConversation).toHaveBeenCalledTimes(1);
      await act(async () => { saveUnread(); });
      await waitFor(() => expect(bookConversation).toHaveBeenCalledTimes(2));
      await act(async () => {});
      expect(screen.queryByText("Who is Mitya?")).toBeNull();
      expect(screen.queryByText("Fyodor’s eldest son.")).toBeNull();
      unmount();
    } finally {
      delete extra.bookConversation;
      delete extra.readChapters;
      delete extra.readMap;
    }
  });
  describe("replies kept in the Notebook follow what is read", () => {
    const reply = (id: string, chapter: number) => ({
      id, question: `About chapter ${chapter}?`, body: `A reply about chapter ${chapter}.`, citations: [], asOfChapter: chapter });
    const shares = (...read: number[]) => ({ chapters: read.map((share, index) => ({ atom: index + 1, read: share })) });
    async function openBook(map: ReturnType<typeof vi.fn>) {
      const extra = mocks as Record<string, unknown>;
      extra.readMap = map;
      extra.readChapters = vi.fn().mockResolvedValue({});
      const view = render(<ReaderPage bookId="b" onBack={() => {}} />);
      await waitFor(() => expect(mocks.init).toHaveBeenCalled());
      await waitFor(() => expect(mocks.onMarkRead).toBeTruthy());
      await waitFor(() => expect(map).toHaveBeenCalled());
      fireEvent.click(screen.getByRole("button", { name: "Notebook" }));
      return view;
    }
    afterEach(() => {
      const extra = mocks as Record<string, unknown>;
      delete extra.readMap;
      delete extra.readChapters;
    });

    // Catches: the fault in 6z's list. A reply kept about a chapter stayed in the Notebook after
    // "I haven't read this" on that chapter; and hiding it must not delete it, nor touch a reply
    // about a chapter still read.
    test("a kept reply leaves with its chapter, says so, and comes back when it is read again", async () => {
      writeSavedReplies("b", activeOfflineOwner(), 0, [reply("one", 1), reply("two", 2)]);
      const map = vi.fn().mockResolvedValue(shares(1, 0.5, 0));
      const { unmount } = await openBook(map);
      expect(await screen.findByText("A reply about chapter 2.")).toBeTruthy();
      expect(screen.getByText("A reply about chapter 1.")).toBeTruthy();
      expect(screen.queryByText(/kept back/)).toBeNull();
      map.mockResolvedValue(shares(1, 0, 0));
      await act(async () => mocks.onMarkRead!(1, false));     // chapter 2: "I haven't read this"
      await waitFor(() => expect(screen.queryByText("A reply about chapter 2.")).toBeNull());
      expect(screen.getByText("A reply about chapter 1.")).toBeTruthy();
      expect(screen.getByText("1 more is kept back until you’ve read those pages again.")).toBeTruthy();
      expect(readSavedReplies("b", activeOfflineOwner(), 0).map((kept) => kept.id)).toEqual(["one", "two"]);
      map.mockResolvedValue(shares(1, 1, 0));
      await act(async () => mocks.onMarkRead!(1, true));
      expect(await screen.findByText("A reply about chapter 2.")).toBeTruthy();
      expect(screen.getByText("A reply about chapter 1.")).toBeTruthy();
      expect(screen.queryByText(/kept back/)).toBeNull();
      unmount();
    });

    // Catches: after "read it fresh" a reply kept in the earlier reading pass either shown at once
    // (its pages no longer count as read) or lost for good (it was filed under the old pass).
    test("a reply kept in an earlier reading pass waits until its chapter is read in this one", async () => {
      mocks.position.mockResolvedValue({ bookmark: 0, cfi: "0:resume", position_epoch: 1 });
      writeSavedReplies("b", activeOfflineOwner(), 0, [reply("old", 1)]);
      const map = vi.fn().mockResolvedValue(shares(0, 0, 0));
      const { unmount } = await openBook(map);
      expect(await screen.findByText("1 saved reply is kept back until you’ve read those pages again.")).toBeTruthy();
      expect(screen.queryByText("A reply about chapter 1.")).toBeNull();
      map.mockResolvedValue(shares(1, 0, 0));
      await act(async () => mocks.onMarkRead!(0, true));
      expect(await screen.findByText("A reply about chapter 1.")).toBeTruthy();
      expect(screen.queryByText(/kept back/)).toBeNull();
      unmount();
    });
  });
  test.each([
    { bookmark: 1, target: "c2.html" },
    { bookmark: 3, target: "c3.html" },
    { bookmark: 30, target: "c3.html" },
  ])(
    "resumes legacy bookmark $bookmark at $target without counting front matter",
    async ({ bookmark, target }) => {
      mocks.position.mockResolvedValue({
        bookmark,
        cfi: null,
        position_epoch: 0,
      });
      mocks.makeBook.mockResolvedValue({
        sections: [
          "cover.html",
          "c1.html",
          "c2.html",
          "c3.html",
          "afterword.html",
        ].map((id) => ({ id })),
        destroy: mocks.destroyBook,
      });
      render(<ReaderPage bookId="b" onBack={() => {}} />);

      await waitFor(() =>
        expect(mocks.goTo).toHaveBeenCalledExactlyOnceWith(target),
      );
      expect(mocks.init).not.toHaveBeenCalled();
    },
  );

  test("keeps the first-section cover for a new book without a CFI", async () => {
    mocks.position.mockResolvedValue({
      bookmark: 0,
      cfi: null,
      position_epoch: 0,
    });
    mocks.makeBook.mockResolvedValue({
      sections: ["cover.html", "c1.html", "c2.html", "c3.html"].map((id) => ({
        id,
      })),
      destroy: mocks.destroyBook,
    });
    render(<ReaderPage bookId="b" onBack={() => {}} />);

    await waitFor(() => expect(mocks.goTo).toHaveBeenCalledExactlyOnceWith(0));
  });

  test("prefers an existing CFI over the legacy chapter bookmark", async () => {
    render(<ReaderPage bookId="b" onBack={() => {}} />);

    await waitFor(() =>
      expect(mocks.init).toHaveBeenCalledExactlyOnceWith({
        lastLocation: "0:resume",
      }),
    );
    expect(mocks.goTo).not.toHaveBeenCalled();
  });

  test.each([
    undefined,
    "",
    "missing.html",
    "https://example.test/chapter.html",
  ])("falls back safely when the legacy resume href is %s", async (href) => {
    mocks.position.mockResolvedValue({
      bookmark: 1,
      cfi: null,
      position_epoch: 0,
    });
    mocks.manifest.mockResolvedValue({
      book_id: "b",
      mode: "file-driven",
      book_type: "novel",
      atoms: [
        { ordinal: 1, href: "c1.html", title: "Chapter 1", char_len: 100 },
        { ordinal: 2, href, title: "Chapter 2", char_len: 100 },
      ],
    });
    mocks.makeBook.mockResolvedValue({
      sections: [
        "cover.html",
        "c1.html",
        "https://example.test/chapter.html",
      ].map((id) => ({ id })),
      destroy: mocks.destroyBook,
    });
    mocks.goTo.mockImplementationOnce(async () => {
      document.querySelector("foliate-view")?.dispatchEvent(
        new CustomEvent("relocate", {
          detail: { cfi: "cover:0", section: { current: 0 } },
        }),
      );
    });
    const { unmount } = render(<ReaderPage bookId="b" onBack={() => {}} />);

    await waitFor(() => expect(mocks.goTo).toHaveBeenCalledExactlyOnceWith(0));
    unmount();
    expect(mocks.putPosition).not.toHaveBeenCalled();
  });

  test("preserves a mapped fragment when legacy chapters share one EPUB file", async () => {
    mocks.position.mockResolvedValue({
      bookmark: 1,
      cfi: null,
      position_epoch: 0,
    });
    mocks.manifest.mockResolvedValue({
      book_id: "b",
      mode: "anchor-driven",
      book_type: "novel",
      atoms: [
        {
          ordinal: 1,
          href: "c1.html#first",
          title: "Chapter 1",
          char_len: 100,
        },
        {
          ordinal: 2,
          href: "c1.html#second",
          title: "Chapter 2",
          char_len: 100,
        },
      ],
    });
    mocks.makeBook.mockResolvedValue({
      sections: [{ id: "cover.html" }, { id: "c1.html" }],
      destroy: mocks.destroyBook,
    });
    render(<ReaderPage bookId="b" onBack={() => {}} />);

    await waitFor(() =>
      expect(mocks.goTo).toHaveBeenCalledExactlyOnceWith("c1.html#second"),
    );
  });

  test("starts the EPUB download while metadata is still pending", async () => {
    const metadata = deferred();
    mocks.books.mockImplementationOnce(async () => {
      await metadata.promise;
      return [];
    });
    const { unmount } = render(<ReaderPage bookId="b" onBack={() => {}} />);
    await waitFor(() => expect(fetch).toHaveBeenCalled());
    expect(mocks.open).not.toHaveBeenCalled();
    await act(async () => {
      metadata.resolve();
    });
    await waitFor(() => expect(mocks.init).toHaveBeenCalled());
    unmount();
    expect(mocks.destroyBook).toHaveBeenCalledOnce();
  });

  test.each(["open", "init"] as const)(
    "releases a book unmounted during engine %s",
    async (stage) => {
      const pending = deferred();
      mocks[stage].mockImplementationOnce(() => pending.promise);
      const { unmount } = render(<ReaderPage bookId="b" onBack={() => {}} />);
      await waitFor(() => expect(mocks[stage]).toHaveBeenCalled());
      unmount();
      await act(async () => {
        pending.resolve();
      });
      expect(mocks.close).toHaveBeenCalledOnce();
      expect(mocks.destroyBook).toHaveBeenCalledOnce();
      if (stage === "open") expect(mocks.init).not.toHaveBeenCalled();
    },
  );

  test("aborts an unfinished EPUB download on unmount", async () => {
    let signal: AbortSignal | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn(
        (_url: string, options: RequestInit) =>
          new Promise((_resolve, reject) => {
            signal = options.signal as AbortSignal;
            signal.addEventListener("abort", () =>
              reject(new DOMException("Aborted", "AbortError")),
            );
          }),
      ),
    );
    const { unmount } = render(<ReaderPage bookId="b" onBack={() => {}} />);
    await waitFor(() => expect(signal).toBeDefined());
    await act(async () => {
      unmount();
    });
    expect(signal?.aborted).toBe(true);
    expect(mocks.makeBook).not.toHaveBeenCalled();
  });

  test("scans read sections only and rejects future candidates defensively", async () => {
    render(<ReaderPage bookId="b" onBack={() => {}} />);
    await waitFor(() => expect(mocks.init).toHaveBeenCalled());
    let results: Array<{ cfi: string }> = [];
    await act(async () => {
      results = await mocks.onSearch!("answer");
    });
    expect(mocks.search).toHaveBeenCalledExactlyOnceWith(
      expect.objectContaining({
        query: "answer",
        index: 0,
        draw: expect.any(Function),
      }),
    );
    expect(results.map((result) => result.cfi)).toEqual(["0:read"]);
    expect(mocks.clearSearch).toHaveBeenCalledOnce();
  });

  test("final-page landing stays pending until an explicit Finish book action is saved", async () => {
    mocks.position.mockResolvedValue({
      bookmark: 2,
      cfi: "2:resume",
      position_epoch: 0,
    });
    mocks.putPosition.mockResolvedValue({
      bookmark: 3,
      cfi: "2:final",
      position_epoch: 0,
      current_chapter: 4,
      chapter_progress: 1,
    });
    render(<ReaderPage bookId="b" onBack={() => {}} />);
    await waitFor(() => expect(mocks.init).toHaveBeenCalled());
    const view = document.querySelector("foliate-view") as TestFoliateView;
    Object.assign(view.renderer, { page: 8, pages: 10 });
    act(() =>
      view.dispatchEvent(
        new CustomEvent("relocate", {
          detail: { cfi: "2:final", section: { current: 2 } },
        }),
      ),
    );
    expect(mocks.putPosition).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Finish book" }));
    await waitFor(() =>
      expect(mocks.putPosition).toHaveBeenCalledWith(
        "b",
        "2:final",
        300,
        3,
        0,
        0,
        expect.any(String),
        expect.any(Number),
      ),
    );
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Finish book" })).toBeNull(),
    );
  });

  test("clears a final-page offer when moving to unmapped backmatter", async () => {
    mocks.position.mockResolvedValue({
      bookmark: 2,
      cfi: "2:resume",
      position_epoch: 0,
    });
    render(<ReaderPage bookId="b" onBack={() => {}} />);
    await waitFor(() => expect(mocks.init).toHaveBeenCalled());
    const view = document.querySelector("foliate-view") as TestFoliateView;
    Object.assign(view.renderer, { page: 8, pages: 10 });
    act(() =>
      view.dispatchEvent(
        new CustomEvent("relocate", {
          detail: { cfi: "2:final", section: { current: 2 } },
        }),
      ),
    );
    expect(screen.getByRole("button", { name: "Finish book" })).toBeTruthy();
    act(() =>
      view.dispatchEvent(
        new CustomEvent("relocate", {
          detail: { cfi: "backmatter", section: { current: 3 } },
        }),
      ),
    );
    expect(screen.queryByRole("button", { name: "Finish book" })).toBeNull();
  });

  // The break: Project Gutenberg's front matter ahead of chapter one, in chapter one's file, was
  // followed as a place in chapter one. Its position was saved, so the book reopened there under
  // "Chapter I" while the shelf said "In chapter 3"; its text became "where you stopped". And a
  // landing on a file with no chapter at all (the cover) threw.
  test("front matter in chapter one's file is not a place in the book", async () => {
    mocks.position.mockResolvedValue({ bookmark: 0, cfi: null, position_epoch: 0, place_chapter: 3 });
    mocks.manifest.mockResolvedValue({
      book_id: "b", mode: "anchor-driven", book_type: "novel",
      atoms: [
        { ordinal: 1, href: "f0.html#c1", title: "Chapter I.", char_len: 100 },
        { ordinal: 2, href: "f0.html#c2", title: "Chapter II.", char_len: 100 },
      ],
    });
    mocks.makeBook.mockResolvedValue({
      sections: [{ id: "cover.html" }, { id: "f0.html" }],
      destroy: mocks.destroyBook,
    });
    const { unmount } = render(<ReaderPage bookId="b" onBack={() => {}} />);
    await waitFor(() => expect(mocks.goTo).toHaveBeenCalled());
    const view = document.querySelector("foliate-view") as TestFoliateView;
    const doc = document.implementation.createHTMLDocument("f0");
    doc.body.innerHTML = "<p>The Project Gutenberg eBook of Split. This eBook is for the use of anyone anywhere.</p>"
      + '<h2 id="c1">Chapter I.</h2><p>Mara arrived in the valley before the snow closed the pass.</p>'
      + '<h2 id="c2">Chapter II.</h2><p>Jonas waited.</p>';
    const land = (paragraph: number, cfi: string, section: number) => {
      const range = doc.createRange();
      range.selectNodeContents(doc.querySelectorAll("p")[paragraph]);
      act(() => { view.dispatchEvent(new CustomEvent("relocate", { detail: { cfi, section: { current: section }, range } })); });
    };
    const stopped = () => Object.keys(localStorage).filter((key) => key.startsWith("mothli.lastPassage:"))
      .map((key) => localStorage.getItem(key)).join("");
    land(0, "cover", 0);
    land(0, "front", 1);
    expect(stopped()).toBe("");
    unmount();
    expect(mocks.putPosition).not.toHaveBeenCalled();

    render(<ReaderPage bookId="b" onBack={() => {}} />);
    await waitFor(() => expect(mocks.goTo).toHaveBeenCalledTimes(2));
    const reopened = [...document.querySelectorAll("foliate-view")].at(-1) as TestFoliateView;
    const range = doc.createRange();
    range.selectNodeContents(doc.querySelectorAll("p")[1]);
    act(() => { reopened.dispatchEvent(new CustomEvent("relocate", { detail: { cfi: "one", section: { current: 1 }, range } })); });
    expect(stopped()).toContain("Mara arrived");
    await waitFor(() => expect(mocks.putPosition).toHaveBeenCalled());
  });

  test("immediate unmount retains an in-flight completion and latest reread cursor without racing replay", async () => {
    mocks.position.mockResolvedValue({ bookmark: 2, cfi: "2:resume", position_epoch: 0 });
    let release!: (saved: unknown) => void;
    mocks.putPosition.mockImplementation(() => new Promise((resolve) => { release = resolve; }));
    const { unmount } = render(<ReaderPage bookId="b" onBack={() => {}} />);
    await waitFor(() => expect(mocks.init).toHaveBeenCalled());
    const view = document.querySelector("foliate-view") as TestFoliateView;
    Object.assign(view.renderer, { page: 8, pages: 10 });
    act(() => view.dispatchEvent(new CustomEvent("relocate", {
      detail: { cfi: "2:finished", section: { current: 2 } },
    })));
    fireEvent.click(screen.getByRole("button", { name: "Finish book" }));
    expect(mocks.putPosition).toHaveBeenCalledTimes(1);
    Object.assign(view.renderer, { page: 1, pages: 10 });
    act(() => view.dispatchEvent(new CustomEvent("relocate", {
      detail: { cfi: "0:rereading", section: { current: 0 } },
    })));
    const fetch = vi.mocked(globalThis.fetch);
    fetch.mockImplementation(async (_url, options) => {
      expect(JSON.parse(options?.body as string)).toMatchObject({
        cfi: "0:rereading", offset: 0, completed_chapter: 3,
        high_water_offset: 300, position_epoch: 0,
      });
      return new Response(JSON.stringify({ bookmark: 3, position_epoch: 0 }), { status: 200 });
    });
    const callsBeforeUnmount = fetch.mock.calls.length;
    unmount();
    expect(pendingPosition("b")).toMatchObject({
      cfi: "0:rereading", offset: 0, completed_chapter: 3, high_water_offset: 300, position_epoch: 0,
    });
    await Promise.resolve();
    expect(fetch).toHaveBeenCalledTimes(callsBeforeUnmount);
    await act(async () => release({ bookmark: 3, cfi: "2:finished", position_epoch: 0,
      current_chapter: 4, chapter_progress: 1 }));
    await waitFor(() => expect(pendingPosition("b")).toBeNull());
    expect(fetch).toHaveBeenCalledTimes(callsBeforeUnmount + 1);
    expect(mocks.putPosition).toHaveBeenCalledTimes(1);
  });

  test("an ordinary queued offline position leaves explicit Finish available without refreshing locked memory", async () => {
    mocks.position.mockResolvedValue({ bookmark: 2, cfi: "2:resume", position_epoch: 0 });
    mocks.putPosition.mockResolvedValueOnce({ bookmark: 2, cfi: "2:final", position_epoch: 0,
      current_chapter: 3, chapter_progress: 0.8, queued: true })
      .mockResolvedValueOnce({ bookmark: 3, cfi: "2:final", position_epoch: 0,
        current_chapter: 4, chapter_progress: 1, queued: true });
    render(<ReaderPage bookId="b" onBack={() => {}} />);
    await waitFor(() => expect(mocks.init).toHaveBeenCalled());
    const view = document.querySelector("foliate-view") as TestFoliateView;
    Object.assign(view.renderer, { page: 8, pages: 10 });
    act(() => view.dispatchEvent(new CustomEvent("relocate", {
      detail: { cfi: "2:final", section: { current: 2 } },
    })));
    act(() => window.dispatchEvent(new Event("pagehide")));
    await waitFor(() => expect(mocks.putPosition).toHaveBeenCalledTimes(1));
    const finish = screen.getByRole("button", { name: "Finish book" }) as HTMLButtonElement;
    await waitFor(() => expect(finish.disabled).toBe(false));
    fireEvent.click(finish);
    await waitFor(() => expect(mocks.putPosition).toHaveBeenCalledWith(
      "b", "2:final", 300, 3, 0, expect.any(Number), expect.any(String), expect.any(Number),
    ));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Finish book" })).toBeNull());
    // Offline intent can update the local progress display, but cannot fetch newly unlocked labels
    // or facts until a canonical server acknowledgment reaches that frontier.
    expect(mocks.manifest).toHaveBeenCalledTimes(1);
    expect(mocks.readerMarks).toHaveBeenCalledTimes(1);
  });

  test("an epoch conflict cancels queued old-page intent and reloads from the new pass", async () => {
    mocks.position.mockResolvedValue({ bookmark: 0, cfi: null, position_epoch: 1 })
      .mockResolvedValueOnce({ bookmark: 2, cfi: "2:resume", position_epoch: 0 });
    let reject!: (error: Error) => void;
    mocks.putPosition.mockImplementation(() => new Promise((_resolve, fail) => { reject = fail; }));
    const { unmount } = render(<ReaderPage bookId="b" onBack={() => {}} />);
    await waitFor(() => expect(mocks.init).toHaveBeenCalled());
    const view = document.querySelector("foliate-view") as TestFoliateView;
    Object.assign(view.renderer, { page: 8, pages: 10 });
    act(() => view.dispatchEvent(new CustomEvent("relocate", {
      detail: { cfi: "2:old-finish", section: { current: 2 } },
    })));
    fireEvent.click(screen.getByRole("button", { name: "Finish book" }));
    act(() => view.dispatchEvent(new CustomEvent("relocate", {
      detail: { cfi: "0:old-reread", section: { current: 0 } },
    })));
    await act(async () => reject(new ApiError("position reset", 409)));
    await waitFor(() => expect(mocks.goTo).toHaveBeenCalledWith(0));
    expect(document.querySelectorAll("foliate-view")).toHaveLength(1);
    expect(mocks.putPosition).toHaveBeenCalledTimes(1);
    expect(mocks.putPosition.mock.calls[0][4]).toBe(0);
    expect(pendingPosition("b")).toBeNull();
    unmount();
    expect(mocks.putPosition).toHaveBeenCalledTimes(1);
  });

  test("reader cleanup never persists an old account's cursor under the newly active owner", async () => {
    render(<ReaderPage bookId="b" onBack={() => {}} />);
    await waitFor(() => expect(mocks.init).toHaveBeenCalled());
    const view = document.querySelector("foliate-view") as TestFoliateView;
    act(() => view.dispatchEvent(new CustomEvent("relocate", {
      detail: { cfi: "0:private-page", section: { current: 0 } },
    })));
    localStorage.setItem("litlet.offline.owner", "05dbebd1-e9e8-4be8-8172-eb65ca8d2a67");
    // pagehide exercises the same owner fence as unmount without requiring an account UI fixture.
    act(() => window.dispatchEvent(new Event("pagehide")));
    expect(pendingPosition("b")).toBeNull();
    expect(mocks.putPosition).not.toHaveBeenCalled();
  });
});

describe("a book that is not on the shelf", () => {
  // Catches: "The book could not be opened: the EPUB could not be fetched (404)" for a book that
  // was removed, or a link to one that was never the reader's.
  test("opens to a kind line and the way back, not an error", async () => {
    mocks.manifest.mockRejectedValue(new ApiError("unknown book", 404));
    mocks.position.mockRejectedValue(new ApiError("unknown book", 404));
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: "unknown book" }), { status: 404 })));
    const onBack = vi.fn();
    localStorage.setItem("rc:reader-marks:b", "[]");
    render(<ReaderPage bookId="b" onBack={onBack} />);
    const alert = await screen.findByRole("alert");
    // Removed on another device: what this one kept of it goes too.
    await waitFor(() => expect(localStorage.getItem("rc:reader-marks:b")).toBeNull());
    expect(alert.textContent).toContain("I can’t find this book on your shelf.");
    expect(alert.textContent).toContain("It may have been removed, here or on another device.");
    expect(alert.textContent).not.toMatch(/EPUB|404|could not be opened/);
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
    const back = screen.getByRole("button", { name: "Back to your shelf" });
    await waitFor(() => expect(document.activeElement).toBe(back));
    fireEvent.click(back);
    expect(onBack).toHaveBeenCalled();
  });

  test("a file that does not arrive for another reason says so in plain words", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("", { status: 503 })));
    render(<ReaderPage bookId="b" onBack={() => {}} />);
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("its pages didn’t arrive. Check your connection and try again.");
    expect(alert.textContent).not.toMatch(/EPUB|503/);
    expect(screen.getByRole("button", { name: "Try again" })).toBeTruthy();
  });

  // Catches: a reader in the middle of a book removed in another tab (or on another device)
  // meeting raw errors on the next page turn instead of being told.
  test.each([
    ["removed in another tab of this browser", () => window.dispatchEvent(new StorageEvent("storage", {
      key: BOOK_REMOVED_KEY, newValue: JSON.stringify({ owner: activeOfflineOwner(), book: "b" }),
    }))],
    ["refused by the server as gone", () => window.dispatchEvent(new CustomEvent(BOOK_GONE_EVENT, { detail: "b" }))],
  ])("an open book %s turns into the same kind line", async (_how, happen) => {
    render(<ReaderPage bookId="b" onBack={() => {}} />);
    await waitFor(() => expect(mocks.init).toHaveBeenCalled());
    // Another book's news changes nothing here.
    act(() => window.dispatchEvent(new CustomEvent(BOOK_GONE_EVENT, { detail: "other" })));
    expect(screen.queryByText("I can’t find this book on your shelf.")).toBeNull();
    act(() => happen());
    expect(await screen.findByText("I can’t find this book on your shelf.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Back to your shelf" })).toBeTruthy();
  });
});

describe("help with a picked passage", () => {
  async function pickAPassage() {
    await waitFor(() => expect(mocks.init).toHaveBeenCalled());
    const paragraph = document.createElement("p");
    paragraph.textContent = "A passage worth keeping.";
    document.body.append(paragraph);
    act(() => document.querySelector("foliate-view")!.dispatchEvent(
      new CustomEvent("load", { detail: { doc: document, index: 0 } })));
    window.getSelection()!.selectAllChildren(paragraph);
    act(() => document.dispatchEvent(new Event("pointerup")));
    const tools = await screen.findByRole("group", { name: "Work with selected text" });
    paragraph.remove();
    return tools;
  }

  beforeEach(() => {
    (TestFoliateView.prototype as unknown as { getCFI: () => string }).getCFI = () => "epubcfi(/6/2!/4/2/1:0)";
  });

  // Catches: Explain, Define and "Talk to Mothli" offered for a picked passage in hosted mode,
  // which has no route for them (ADR 0043), each ending in "I can't help with a picked passage";
  // or the server asked about one at all.
  test("hosted mode offers only what works with a picked passage and asks the server nothing", async () => {
    const extra = mocks as Record<string, unknown>;
    extra.selectionAction = vi.fn();
    try {
      render(<ReaderPage bookId="b" onBack={() => {}} hosted />);
      const tools = await pickAPassage();
      expect(within(tools).queryByRole("button", { name: "Talk to Mothli" })).toBeNull();
      expect(within(tools).queryByRole("button", { name: "More passage tools" })).toBeNull();
      expect(within(tools).getByRole("button", { name: /Highlight/ })).toBeTruthy();
      expect(within(tools).getByRole("button", { name: "Note" })).toBeTruthy();
      expect(extra.selectionAction).not.toHaveBeenCalled();
      expect(vi.mocked(globalThis.fetch).mock.calls.map(([url]) => String(url))
        .filter((url) => url.includes("selection-action"))).toEqual([]);
    } finally {
      delete extra.selectionAction;
    }
  });

  test("local mode keeps every passage tool, as before", async () => {
    render(<ReaderPage bookId="b" onBack={() => {}} />);
    const tools = await pickAPassage();
    expect(within(tools).getByRole("button", { name: "Talk to Mothli" })).toBeTruthy();
    expect(within(tools).getByRole("button", { name: "More passage tools" })).toBeTruthy();
  });
});

describe("the note after a chapter", () => {
  // Catches: "Tell me" in hosted mode asking for the chapter look-back, a route hosted mode does
  // not have (ADR 0043), so every reader's first "Tell me" is a failed request before the chat.
  test("hosted mode asks the chat about the chapter and never the look-back", async () => {
    const extra = mocks as Record<string, unknown>;
    extra.chapterCloseout = vi.fn();
    // Hosted mode replies only as a stream (ADR 0043 removed the non-streamed /ask there).
    extra.askBookStream = vi.fn(async () => ({ as_of_chapter: 2, insufficient_evidence: true,
      claims: [], citations: [], cost: null, read_frontier: hostedPoint }));
    mocks.readFrontier.mockResolvedValue(hostedPoint as never);
    mocks.putPosition.mockResolvedValue({ bookmark: 2, cfi: "1:end", position_epoch: 0,
      current_chapter: 3, chapter_progress: 0 });
    try {
      render(<ReaderPage bookId="b" onBack={() => {}} hosted />);
      await waitFor(() => expect(mocks.init).toHaveBeenCalled());
      const view = document.querySelector("foliate-view") as TestFoliateView;
      act(() => view.dispatchEvent(new CustomEvent("relocate", {
        detail: { cfi: "2:start", section: { current: 2 } },
      })));
      fireEvent.click(await screen.findByRole("button", { name: "Tell me" }));
      await waitFor(() => expect(extra.askBookStream).toHaveBeenCalled());
      expect((extra.askBookStream as ReturnType<typeof vi.fn>).mock.calls[0][1])
        .toBe("What did you notice in Chapter 2?");
      expect(extra.chapterCloseout).not.toHaveBeenCalled();
      expect(mocks.askBook).not.toHaveBeenCalled();
    } finally {
      delete extra.chapterCloseout;
      delete extra.askBookStream;
    }
  });
});

// ADR 0045 step 5: the reading screen of a reader whose free weeks or plan are over, and of one
// who has not started.
describe("the reading screen and where the reader stands with Mothli", () => {
  const extra = mocks as Record<string, unknown>;
  const finishChapterTwo = async () => {
    localStorage.setItem("rc:starting-place-asked:b", "yes");
    mocks.putPosition.mockResolvedValue({ bookmark: 2, cfi: "1:end", position_epoch: 0,
      current_chapter: 3, chapter_progress: 0 });
    render(<ReaderPage bookId="b" onBack={() => {}} hosted />);
    await waitFor(() => expect(mocks.init).toHaveBeenCalled());
    const view = document.querySelector("foliate-view") as TestFoliateView;
    act(() => view.dispatchEvent(new CustomEvent("relocate", {
      detail: { cfi: "2:start", section: { current: 2 } },
    })));
  };
  afterEach(() => {
    for (const name of ["graph", "notes", "startMothli", "askBookStream", "bookConversation"]) delete extra[name];
    setMothliStanding(null);
  });

  // Catches: the corner note offering an ended reader "Want to hear what I noticed?", names in the
  // page looked up (and refused), or anything about the gate on the page itself.
  test("ended: the note keeps its first part and drops the offer; no names are looked up", async () => {
    const asked = { graph: vi.fn(), notes: vi.fn(), startMothli: vi.fn(), askBookStream: vi.fn(),
      bookConversation: vi.fn() };
    Object.assign(extra, asked);
    setMothliStanding({ state: "ended", why: "time", ending_note: false });
    await finishChapterTwo();
    const note = await screen.findByText("That’s Chapter 2 done!");
    expect(note.textContent).toBe("That’s Chapter 2 done!");
    expect(screen.queryByRole("button", { name: "Tell me" })).toBeNull();
    expect(document.body.textContent).not.toMatch(/Want to hear what I noticed|Keep talking with Mothli|free weeks/);
    // Reading is untouched: the place is kept.
    expect(mocks.putPosition).toHaveBeenCalled();
    // Opening Mothli shows the invitation, and asks nothing.
    fireEvent.click(screen.getByRole("button", { name: "Talk to Mothli" }));
    expect(await screen.findByRole("button", { name: "Keep talking with Mothli" })).toBeTruthy();
    for (const [name, call] of Object.entries(asked)) expect(call, name).not.toHaveBeenCalled();
  });

  // Catches: the app's own fetches (the cast for names in the page, the notes after a chapter)
  // starting a new reader's free weeks.
  test("new: the automatic fetches happen as before and never call the start route", async () => {
    const asked = {
      graph: vi.fn().mockResolvedValue({ as_of_chapter: 2, characters: [], relationships: [] }),
      notes: vi.fn().mockResolvedValue({ as_of_chapter: 2, cast: [], chapters: [] }),
      startMothli: vi.fn(),
    };
    Object.assign(extra, asked);
    setMothliStanding({ state: "new", why: null, ending_note: false });
    await finishChapterTwo();
    expect(await screen.findByRole("button", { name: "Tell me" })).toBeTruthy();
    await waitFor(() => expect(asked.notes).toHaveBeenCalled());
    expect(asked.graph).toHaveBeenCalled();
    expect(asked.startMothli).not.toHaveBeenCalled();
  });
});

// 2026-11-20 walk-through: a reader imports a book they are halfway through. Nothing asked where
// they were, and once they found "I've read up to here" a pop-up cheered "a quarter of the way
// through" at 49%.
describe("a book opened here for the first time", () => {
  const extra = mocks as Record<string, unknown>;
  let readChapters: ReturnType<typeof vi.fn>;
  beforeEach(() => {
    readChapters = vi.fn().mockResolvedValue({});
    extra.readChapters = readChapters;
    extra.readMap = vi.fn().mockResolvedValue({ chapters: [] });
    mocks.position.mockResolvedValue({ bookmark: 0, cfi: "0:start", position_epoch: 0 });
  });
  afterEach(() => {
    delete extra.readChapters;
    delete extra.readMap;
  });

  test("asks once where the reader is, counts the chapters before it by the read map, and does not cheer", async () => {
    const { unmount } = render(<ReaderPage bookId="b" onBack={() => {}} />);
    const question = await screen.findByRole("dialog", { name: "Already partway through?" });
    fireEvent.change(within(question).getByLabelText("I’m on"), { target: { value: "2" } });
    expect(question.textContent).toMatch(/Chapters 1 and 2 will count as read\. We’ll pick up at the start of chapter 3\./);
    // The server's counter after the reader's word: two chapters finished.
    mocks.position.mockResolvedValue({ bookmark: 2, cfi: "0:start", position_epoch: 0 });
    fireEvent.click(within(question).getByRole("button", { name: "take me to chapter 3" }));
    await waitFor(() => expect(readChapters).toHaveBeenCalledWith("b", [1, 2], true, "told"));
    await waitFor(() => expect(mocks.goTo).toHaveBeenCalledWith("c3.html"));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Already partway through?" })).toBeNull());
    await waitFor(() => expect(mocks.position.mock.calls.length).toBeGreaterThanOrEqual(2));
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)); });
    // Counted, not celebrated: no "done!", no milestone.
    expect(document.body.textContent).not.toMatch(/done!|of the way through|Halfway/);
    unmount();
    // Never asked again for this book.
    render(<ReaderPage bookId="b" onBack={() => {}} />);
    await waitFor(() => expect(mocks.init).toHaveBeenCalledTimes(2));
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)); });
    expect(screen.queryByRole("dialog", { name: "Already partway through?" })).toBeNull();
  });

  // 2026-11-20 recheck: chapters 30 and 31 share a file and 31 begins partway down a page. "take
  // me to chapter 31" showed that page and said "Chapter 30": its first lines end chapter 30.
  test("lands in the picked chapter when it begins partway down a page, and stays there when laid out again", async () => {
    const atoms = ["one", "two", "three"].map((id, index) => ({
      ordinal: index + 1, href: `c1.html#${id}`, title: `Chapter ${index + 1}`, char_len: 100 }));
    mocks.manifest.mockResolvedValue({ book_id: "b", mode: "anchor-driven", book_type: "novel", atoms });
    mocks.makeBook.mockResolvedValue({ sections: [{ id: "c1.html" }],
      toc: atoms.map((atom) => ({ href: atom.href })), destroy: mocks.destroyBook });
    const doc = document.implementation.createHTMLDocument("chapters");
    doc.body.innerHTML = "<h2 id='one'>One</h2><p>First.</p><h2 id='two'>Two</h2><p>The end of two.</p>"
      + "<h2 id='three'>Three</h2><p>Three begins.</p>";
    // The page that shows the last lines of chapter 2 and the heading of chapter 3.
    const page = doc.createRange();
    page.setStart(doc.querySelectorAll("p")[1].firstChild!, 0);
    page.setEnd(doc.getElementById("three")!.firstChild!, 5);
    const view = () => document.querySelector("foliate-view") as TestFoliateView;
    const show = (range: Range) => act(() => view().dispatchEvent(new CustomEvent("relocate", {
      detail: { cfi: "epubcfi(/6/2!/4/4/1:0)", section: { current: 0 }, range } })));
    mocks.goTo.mockImplementation(async (target: unknown) => {
      if (target === "c1.html#three") show(page);
    });
    const { unmount } = render(<ReaderPage bookId="b" onBack={() => {}} />);
    const question = await screen.findByRole("dialog", { name: "Already partway through?" });
    fireEvent.change(within(question).getByLabelText("I’m on"), { target: { value: "2" } });
    mocks.position.mockResolvedValue({ bookmark: 2, cfi: "epubcfi(/6/2!/4/4/1:0)", position_epoch: 0 });
    fireEvent.click(within(question).getByRole("button", { name: "take me to chapter 3" }));
    await waitFor(() => expect(mocks.goTo).toHaveBeenCalledWith("c1.html#three"));
    await waitFor(() => expect(document.body.textContent).toMatch(/Chapter 3 of 3/));
    // A resize lays the same page out again: still chapter 3.
    show(page.cloneRange());
    expect(document.body.textContent).toMatch(/Chapter 3 of 3/);
    // Turning back to a page of chapter 2 alone is chapter 2 again.
    const back = doc.createRange();
    back.selectNodeContents(doc.querySelectorAll("p")[1]);
    show(back);
    expect(document.body.textContent).toMatch(/Chapter 2 of 3/);
    expect(document.body.textContent).not.toMatch(/Chapter 3 of 3/);
    // The book closed on that page and opened again: still chapter 3.
    show(page.cloneRange());
    unmount();
    render(<ReaderPage bookId="b" onBack={() => {}} />);
    await waitFor(() => expect(mocks.init).toHaveBeenCalledTimes(2));
    show(page.cloneRange());
    expect(document.body.textContent).toMatch(/Chapter 3 of 3/);
  });

  test("“I’m just starting” counts nothing and is not asked again", async () => {
    const { unmount } = render(<ReaderPage bookId="b" onBack={() => {}} />);
    const question = await screen.findByRole("dialog", { name: "Already partway through?" });
    fireEvent.click(within(question).getByRole("button", { name: "I’m just starting" }));
    expect(screen.queryByRole("dialog", { name: "Already partway through?" })).toBeNull();
    expect(readChapters).not.toHaveBeenCalled();
    unmount();
    render(<ReaderPage bookId="b" onBack={() => {}} />);
    await waitFor(() => expect(mocks.init).toHaveBeenCalledTimes(2));
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)); });
    expect(screen.queryByRole("dialog", { name: "Already partway through?" })).toBeNull();
  });

  test("a book with anything read in it is not asked about", async () => {
    extra.readMap = vi.fn().mockResolvedValue({ chapters: [{ atom: 1, read: 0.4 }] });
    render(<ReaderPage bookId="b" onBack={() => {}} />);
    await waitFor(() => expect(mocks.init).toHaveBeenCalled());
    await waitFor(() => expect(extra.readMap).toHaveBeenCalled());
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)); });
    expect(screen.queryByRole("dialog", { name: "Already partway through?" })).toBeNull();
  });

  test("a chapter finished by reading is still celebrated", async () => {
    localStorage.setItem("rc:starting-place-asked:b", "yes");
    mocks.position.mockResolvedValue({ bookmark: 1, cfi: "0:resume", position_epoch: 0 });
    mocks.putPosition.mockResolvedValue({ bookmark: 2, cfi: "1:end", position_epoch: 0,
      current_chapter: 3, chapter_progress: 0 });
    render(<ReaderPage bookId="b" onBack={() => {}} />);
    await waitFor(() => expect(mocks.init).toHaveBeenCalled());
    const view = document.querySelector("foliate-view") as TestFoliateView;
    act(() => view.dispatchEvent(new CustomEvent("relocate", {
      detail: { cfi: "2:start", section: { current: 2 } },
    })));
    expect(await screen.findByText(/That’s Chapter 2 done!/)).toBeTruthy();
  });
});

// 2026-11-20 recheck: the streak said "1" as soon as the book opened, before anything was read.
describe("the reading streak", () => {
  test("opening a book is not a reading day; a page read is", async () => {
    let now = 1_000_000;
    const clock = vi.spyOn(Date, "now").mockImplementation(() => now);
    try {
      render(<ReaderPage bookId="b" onBack={() => {}} />);
      await waitFor(() => expect(mocks.init).toHaveBeenCalled());
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)); });
      expect(screen.queryByText(/-day streak$/)).toBeNull();
      const doc = document.implementation.createHTMLDocument("chapter");
      doc.body.innerHTML = "<p>Some words of chapter two to read.</p><p>And the next page.</p>";
      const page = (index: number) => {
        const range = doc.createRange();
        range.selectNodeContents(doc.querySelectorAll("p")[index]);
        return range;
      };
      const view = document.querySelector("foliate-view") as TestFoliateView;
      act(() => view.dispatchEvent(new CustomEvent("relocate", {
        detail: { cfi: "epubcfi(/6/4!/4/2/1:0)", section: { current: 1 }, range: page(0) } })));
      expect(screen.queryByText(/-day streak$/)).toBeNull();
      now += 60_000;      // a minute on that page, then the next one
      act(() => view.dispatchEvent(new CustomEvent("relocate", {
        detail: { cfi: "epubcfi(/6/4!/4/4/1:0)", section: { current: 1 }, range: page(1) } })));
      // Said as the shelf says it (L8 of the plan walk), the number alone on show.
      await waitFor(() => expect(screen.getByText("1-day streak").parentElement?.getAttribute("title")).toBe("1-day streak"));
      expect(screen.getByText("1-day streak").parentElement?.textContent).toBe("1-day streak1");
    } finally {
      clock.mockRestore();
    }
  });
});

// L7 of the plan walk: a 404 in the console on every book opened in hosted mode, the offer of
// chapters read in another app asked of a server that keeps no such history and has no route.
describe("the offer of chapters read elsewhere", () => {
  test("is asked only where the server keeps reading history (local mode), never hosted", async () => {
    const extra = mocks as Record<string, unknown>;
    extra.readMap = vi.fn().mockResolvedValue({ chapters: [{ atom: 1, read: 0 }] });
    extra.readOffer = vi.fn().mockResolvedValue({ offer: null });
    try {
      const hosted = render(<ReaderPage bookId="b" onBack={() => {}} hosted />);
      await waitFor(() => expect(extra.readMap).toHaveBeenCalled());
      await act(async () => {});
      expect(extra.readOffer).not.toHaveBeenCalled();
      hosted.unmount();
      render(<ReaderPage bookId="b" onBack={() => {}} />);
      await waitFor(() => expect(extra.readOffer).toHaveBeenCalledWith("b"));
    } finally {
      delete extra.readMap;
      delete extra.readOffer;
    }
  });
});

const hostedPoint = { id: "a".repeat(64), atom: 2, offset: 0, position_epoch: 0, available_chapter: 2 };

describe("a server without Mothli's chat", () => {
  // Catches: a hosted server with no chat (its AI accounts not filled in) still offering the moth,
  // "Tell me" after a chapter, the "/" shortcut or the panel, each ending in "I couldn't get a
  // reply. Try again?"; or the reader asking the server anything that can only fail.
  test("shows no way into a chat and asks the server nothing about one", async () => {
    const extra = mocks as Record<string, unknown>;
    const asked = {
      askBookStream: vi.fn(), bookConversation: vi.fn(), chapterCloseout: vi.fn(),
      graph: vi.fn(), notes: vi.fn(), catchMeUp: vi.fn(), character: vi.fn(),
    };
    Object.assign(extra, asked);
    mocks.putPosition.mockResolvedValue({ bookmark: 2, cfi: "1:end", position_epoch: 0,
      current_chapter: 3, chapter_progress: 0 });
    try {
      render(<ReaderPage bookId="b" onBack={() => {}} hosted chat={false} />);
      await waitFor(() => expect(mocks.init).toHaveBeenCalled());
      const view = document.querySelector("foliate-view") as TestFoliateView;
      act(() => view.dispatchEvent(new CustomEvent("relocate", {
        detail: { cfi: "2:start", section: { current: 2 } },
      })));
      await waitFor(() => expect(mocks.putPosition).toHaveBeenCalled());
      fireEvent.keyDown(document, { key: "/" });
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)); });
      expect(screen.queryByRole("button", { name: "Talk to Mothli" })).toBeNull();
      expect(screen.queryByRole("button", { name: "Tell me" })).toBeNull();
      expect(screen.queryByLabelText("Message Mothli")).toBeNull();
      for (const [name, call] of Object.entries(asked)) expect(call, name).not.toHaveBeenCalled();
      expect(mocks.askBook).not.toHaveBeenCalled();
      expect(vi.mocked(globalThis.fetch).mock.calls.map(([url]) => String(url))
        .filter((url) => /\/ask|conversation|graph|notes|catch-me-up/.test(url))).toEqual([]);
      // Reading stays: the place is still kept.
      expect(mocks.putPosition).toHaveBeenCalled();
    } finally {
      for (const name of Object.keys(asked)) delete extra[name];
    }
  });

  test("a server with the chat still offers it, as before", async () => {
    render(<ReaderPage bookId="b" onBack={() => {}} hosted chat />);
    await waitFor(() => expect(mocks.init).toHaveBeenCalled());
    expect(await screen.findByRole("button", { name: "Talk to Mothli" })).toBeTruthy();
  });
});
