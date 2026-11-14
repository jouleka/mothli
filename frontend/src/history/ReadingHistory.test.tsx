import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, test, vi } from "vitest";
import { axeAA } from "../test-a11y";
import { ReadingHistory } from "./ReadingHistory";

const mocks = vi.hoisted(() => ({
  history: vi.fn(),
  historyEntry: vi.fn(),
  importHistory: vi.fn(),
  forgetHistory: vi.fn(),
  addClassic: vi.fn(),
  importHistoryDatabases: vi.fn(),
  importReadwise: vi.fn(),
  importBook: vi.fn(),
  setHistoryBook: vi.fn(),
  historyGlimpse: vi.fn(),
}));
vi.mock("../api", () => ({ api: mocks }));

const nothing = {
  books: 0,
  highlights: 0,
  with_highlights: 0,
  free: 0,
  by_status: { read: 0, reading: 0, want: 0, abandoned: 0 },
  matches: 0,
  entries: [],
};
const austen = {
  id: 1,
  title: "Pride and Prejudice",
  author: "Jane Austen",
  status: "read" as const,
  rating: 5,
  finished_on: "2021-05-03",
  added_on: "2021-04-01",
  classic_id: "1342",
  book_id: null,
  sources: ["goodreads", "kindle"],
  highlights: 1,
  has_review: true,
};
const hugo = {
  id: 2,
  title: "The Seven Husbands of Evelyn Hugo",
  author: "Taylor Jenkins Reid",
  status: "want" as const,
  rating: null,
  finished_on: null,
  added_on: "2023-08-01",
  classic_id: null,
  book_id: null,
  sources: ["goodreads"],
  highlights: 0,
  has_review: false,
};
const full = {
  books: 2,
  highlights: 1,
  with_highlights: 1,
  free: 1,
  by_status: { read: 1, reading: 0, want: 1, abandoned: 0 },
  matches: 2,
  entries: [austen, hugo],
};
const detail = {
  ...austen,
  review: "Still the best.",
  isbn: "9780141439518",
  marks: [{ id: 9, text: "It is a truth universally acknowledged", note: "The famous opening", location: "Location 62-64", made_at: "2024-03-01T21:13:02", source: "kindle" }],
};

const GOODREADS = [
  "Title,Author,ISBN13,My Rating,Date Read,Date Added,Exclusive Shelf,My Review",
  '"Pride and Prejudice","Jane Austen",,5,2021/05/03,2021/04/01,read,"Still the best."',
  '"The Seven Husbands of Evelyn Hugo","Taylor Jenkins Reid",,0,,2023/08/01,to-read,',
].join("\n");
const CLIPPINGS = [
  "Pride and Prejudice (Austen, Jane)",
  "- Your Highlight on Location 62-64 | Added on Friday, March 1, 2024 9:13:02 PM",
  "",
  "It is a truth universally acknowledged",
  "==========",
  "",
].join("\r\n");

beforeEach(() => {
  mocks.history.mockReset().mockResolvedValue(nothing);
  mocks.historyEntry.mockReset().mockResolvedValue(detail);
  mocks.importHistory.mockReset().mockResolvedValue({ books_added: 0, books_updated: 0, highlights_added: 0, skipped: 0 });
  mocks.forgetHistory.mockReset().mockResolvedValue(undefined);
  mocks.addClassic.mockReset();
  mocks.importHistoryDatabases.mockReset();
  mocks.importReadwise.mockReset();
  mocks.importBook.mockReset();
  mocks.setHistoryBook.mockReset().mockResolvedValue({});
  mocks.historyGlimpse.mockReset().mockResolvedValue({ mark: null });
});

describe("Your reading history", () => {
  test("starts by asking where you have been reading, and shows what to fetch from each app", async () => {
    const user = userEvent.setup();
    const { container } = render(<ReadingHistory onBack={() => {}} onOpen={() => {}} />);
    expect(screen.getByRole("heading", { level: 1, name: "Your reading history" })).toBeTruthy();
    expect(await screen.findByRole("heading", { name: "Where have you been reading?" })).toBeTruthy();
    expect(screen.getByText("Tell me where you’ve been reading.")).toBeTruthy();
    const apps = within(screen.getByRole("list", { name: "Reading apps" }));
    expect(apps.getAllByRole("button").map((button) => button.textContent)).toEqual([
      "GGoodreadsYour list, ratings, reviews",
      "KKindleHighlights and notes",
      "KKoboLibrary, progress, highlights",
      "AApple BooksLibrary, progress, highlights",
      "SStoryGraphYour list, ratings, reviews",
      "RReadwiseEvery highlight, in one go",
      "LLibbyHighlights and loans",
      "SSomewhere elseKOReader, Calibre, your own list",
    ]);
    // nothing is asked for until an app is picked; the drop zone is always there
    expect(screen.getByText("Drop your files here")).toBeTruthy();
    expect(screen.queryByRole("region", { name: /How to bring/ })).toBeNull();
    expect(screen.queryByLabelText("Your Readwise access token")).toBeNull();

    const kindle = apps.getByRole("button", { name: /Kindle/ });
    await user.click(kindle);
    expect(kindle.getAttribute("aria-pressed")).toBe("true");
    const how = within(screen.getByRole("region", { name: "How to bring your Kindle history" }));
    expect(how.getByRole("heading", { name: "On a Kindle e-reader" })).toBeTruthy();
    expect(how.getByRole("heading", { name: "In the Kindle app on a phone or tablet" })).toBeTruthy();
    expect(how.getByText(/My Clippings.txt/)).toBeTruthy();
    expect(screen.getByText("Drop your Kindle file here")).toBeTruthy();

    await user.click(apps.getByRole("button", { name: /Kobo/ }));
    expect(screen.getByText(/KoboReader.sqlite/)).toBeTruthy();
    expect(screen.queryByText(/My Clippings.txt/)).toBeNull();
    await user.click(apps.getByRole("button", { name: /Apple Books/ }));
    expect(screen.getByText(/Apple gives no way to do this from an iPhone or iPad/)).toBeTruthy();
    await user.click(apps.getByRole("button", { name: /Somewhere else/ }));
    expect(screen.getByText(/Google Play Books isn’t possible yet/)).toBeTruthy();
    expect(screen.getByText("Drop your file here")).toBeTruthy();

    expect(screen.getByText(/any book files you have without copy\s+protection \(EPUB, FB2, MOBI or AZW3\)/)).toBeTruthy();
    expect(screen.getByText(/usually copy-protected,\s+and those files can’t be opened here or anywhere else but their own app/)).toBeTruthy();
    expect(await axeAA(container)).toHaveNoViolations();
  });

  test("reads the chosen files, says what is in each, and imports them in batches", async () => {
    const user = userEvent.setup({ applyAccept: false });
    mocks.importHistory
      .mockResolvedValueOnce({ books_added: 2, books_updated: 0, highlights_added: 0, skipped: 0 })
      .mockResolvedValueOnce({ books_added: 0, books_updated: 0, highlights_added: 1, skipped: 0 });
    render(<ReadingHistory onBack={() => {}} onOpen={() => {}} />);
    const input = await screen.findByLabelText("Choose export files");
    await user.upload(input, [
      new File([GOODREADS], "goodreads_library_export.csv", { type: "text/csv" }),
      new File([CLIPPINGS], "My Clippings.txt", { type: "text/plain" }),
      new File(["dear diary"], "diary.txt", { type: "text/plain" }),
    ]);
    const files = within(await screen.findByRole("list", { name: "Chosen files" }));
    expect(files.getByText("Goodreads: 2 books")).toBeTruthy();
    expect(files.getByText("Kindle: 1 highlight")).toBeTruthy();
    expect(files.getByText(/couldn’t tell which app this file is from/)).toBeTruthy();

    mocks.history.mockResolvedValue(full);
    await user.click(screen.getByRole("button", { name: "Import 2 books and 1 highlight" }));
    const outcome = await screen.findByRole("status");
    expect(outcome.textContent).toContain("Brought in 2 new books and 1 new highlight.");
    expect(outcome.textContent).toContain("1 book on your list is free to read here.");
    expect(mocks.importHistory).toHaveBeenCalledTimes(2);
    const [first, second] = mocks.importHistory.mock.calls.map((call) => call[0]);
    expect(first.source).toBe("goodreads");
    expect(first.books.map((book: { title: string; status: string }) => [book.title, book.status])).toEqual([
      ["Pride and Prejudice", "read"],
      ["The Seven Husbands of Evelyn Hugo", "want"],
    ]);
    expect(second).toEqual({
      source: "kindle",
      books: [],
      highlights: [
        {
          title: "Pride and Prejudice",
          author: "Jane Austen",
          text: "It is a truth universally acknowledged",
          note: null,
          location: "Location 62-64",
          position: 62,
          made_at: "2024-03-01T21:13:02",
        },
      ],
    });
    // the list appears beneath, and the outcome stays on screen
    expect(await screen.findByRole("button", { name: "Pride and Prejudice, Jane Austen" })).toBeTruthy();
    expect(screen.getByRole("status")).toBeTruthy();
  });

  test("an import that stops part-way says so and that it is safe to run again", async () => {
    const user = userEvent.setup({ applyAccept: false });
    mocks.importHistory.mockRejectedValue(new Error("offline"));
    render(<ReadingHistory onBack={() => {}} onOpen={() => {}} />);
    await user.upload(await screen.findByLabelText("Choose export files"), new File([GOODREADS], "g.csv"));
    await user.click(await screen.findByRole("button", { name: "Import 2 books" }));
    expect((await screen.findByRole("alert")).textContent).toContain("you can run it again safely");
    expect(screen.getByRole("button", { name: "Import 2 books" })).toBeTruthy();
  });

  test("lists what was brought in, with filters, and says which books are free to read here", async () => {
    const user = userEvent.setup();
    mocks.history.mockImplementation(async (search: { status?: string; only?: string } = {}) =>
      search.only === "free" ? { ...full, matches: 1, entries: [austen] } : full,
    );
    const { container } = render(<ReadingHistory onBack={() => {}} onOpen={() => {}} />);
    const row = await screen.findByRole("button", { name: "Pride and Prejudice, Jane Austen" });
    expect(within(row).getByText("Free to read here")).toBeTruthy();
    expect(within(row).getByText("Read May 3, 2021")).toBeTruthy();
    expect(within(row).getByText("1 highlight")).toBeTruthy();
    expect(within(row).getByRole("img", { name: "5 out of 5 stars" })).toBeTruthy();
    expect(screen.getByText(/2 books and 1 highlight so far/)).toBeTruthy();
    // the open book counts what came across, and each count is a way in
    const glance = within(screen.getByRole("list", { name: "At a glance" }));
    expect(glance.getAllByRole("button").map((button) => button.getAttribute("aria-label"))).toEqual([
      "1 book read", "1 to read", "1 passage marked", "1 free to read here",
    ]);
    // with a history, the import desk waits behind a button
    expect(screen.queryByRole("heading", { name: "Where have you been reading?" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Bring more in" }));
    expect(screen.getByRole("heading", { name: "Where have you been reading?" })).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Close import" }));
    expect(screen.queryByRole("heading", { name: "Where have you been reading?" })).toBeNull();
    expect(screen.queryByRole("button", { name: /Set aside/ })).toBeNull();
    expect(await axeAA(container)).toHaveNoViolations();

    await user.click(screen.getByRole("button", { name: /Free to read here 1/ }));
    await waitFor(() => expect(screen.queryByRole("button", { name: /Evelyn Hugo/ })).toBeNull());
    expect(mocks.history).toHaveBeenLastCalledWith({ status: "", only: "free", q: "", offset: 0, limit: 60 });
    await user.click(screen.getByRole("button", { name: /Want to read 1/ }));
    await waitFor(() => expect(mocks.history).toHaveBeenLastCalledWith({ status: "want", only: "", q: "", offset: 0, limit: 60 }));
  });

  test("a title opens with its review and highlights, and a free book can be added and read", async () => {
    const user = userEvent.setup();
    const onOpen = vi.fn();
    mocks.history.mockResolvedValue(full);
    mocks.addClassic.mockResolvedValue({ book_id: "bkpride" });
    render(<ReadingHistory onBack={() => {}} onOpen={onOpen} />);
    await user.click(await screen.findByRole("button", { name: "Pride and Prejudice, Jane Austen" }));
    const sheet = screen.getByRole("dialog", { name: "Pride and Prejudice" });
    expect(await within(sheet).findByText("Still the best.")).toBeTruthy();
    expect(within(sheet).getByText("It is a truth universally acknowledged")).toBeTruthy();
    expect(within(sheet).getByText("The famous opening")).toBeTruthy();
    expect(within(sheet).getByText("Location 62-64 · Mar 1, 2024 · Kindle")).toBeTruthy();
    expect(within(sheet).getByText("Read · May 3, 2021 · Goodreads, Kindle")).toBeTruthy();
    await user.click(within(sheet).getByRole("button", { name: "Add it free" }));
    expect(mocks.addClassic).toHaveBeenCalledWith("1342");
    await user.click(await within(sheet).findByRole("button", { name: "Read it again" }));
    expect(onOpen).toHaveBeenCalledWith("bkpride");
  });

  test("a book we do not have can be read from the reader's own file, added right there", async () => {
    const user = userEvent.setup({ applyAccept: false });
    const onOpen = vi.fn();
    mocks.history.mockResolvedValue(full);
    mocks.historyEntry.mockResolvedValue({ ...hugo, review: null, isbn: null, marks: [] });
    mocks.importBook.mockResolvedValue({ book_id: "bkhugo" });
    render(<ReadingHistory onBack={() => {}} onOpen={onOpen} />);
    await user.click(await screen.findByRole("button", { name: "The Seven Husbands of Evelyn Hugo, Taylor Jenkins Reid" }));
    const sheet = screen.getByRole("dialog");
    expect(within(sheet).getByText(/you can read your own/)).toBeTruthy();
    expect(within(sheet).getByText(/Only copy-protected files can’t be opened/)).toBeTruthy();
    expect(within(sheet).queryByRole("button", { name: "Add it free" })).toBeNull();
    expect(within(sheet).getByRole("button", { name: "Add my copy" })).toBeTruthy();

    const file = new File(["PK epub bytes"], "evelyn-hugo.epub", { type: "application/epub+zip" });
    await user.upload(within(sheet).getByLabelText("Choose your copy of The Seven Husbands of Evelyn Hugo"), file);
    await user.click(await within(sheet).findByRole("button", { name: "Start reading" }));
    expect(mocks.importBook).toHaveBeenCalledTimes(1);
    expect(mocks.setHistoryBook).toHaveBeenCalledWith(2, "bkhugo");
    expect(onOpen).toHaveBeenCalledWith("bkhugo");
  });

  test("a file already on the shelf is tied to the title, and a copy-protected or unreadable one says why", async () => {
    const user = userEvent.setup({ applyAccept: false });
    mocks.history.mockResolvedValue(full);
    mocks.historyEntry.mockResolvedValue({ ...hugo, review: null, isbn: null, marks: [] });
    render(<ReadingHistory onBack={() => {}} onOpen={() => {}} />);
    await user.click(await screen.findByRole("button", { name: /Evelyn Hugo/ }));
    const sheet = screen.getByRole("dialog");
    const picker = within(sheet).getByLabelText("Choose your copy of The Seven Husbands of Evelyn Hugo");

    mocks.importBook.mockRejectedValueOnce(new Error("This book is copy-protected (DRM), so it can’t be opened here."));
    await user.upload(picker, new File(["x"], "locked.epub", { type: "application/epub+zip" }));
    expect((await within(sheet).findByRole("alert")).textContent).toContain("copy-protected");
    expect(within(sheet).getByRole("button", { name: "Add my copy" })).toBeTruthy();

    await user.upload(picker, new File(["x"], "scan.pdf", { type: "application/pdf" }));
    expect((await within(sheet).findByRole("alert")).textContent).toMatch(/PDF/);

    mocks.importBook.mockRejectedValueOnce(new Error("book already imported as 'bkold'"));
    await user.upload(picker, new File(["x"], "evelyn.epub", { type: "application/epub+zip" }));
    expect(await within(sheet).findByRole("button", { name: "Start reading" })).toBeTruthy();
    expect(mocks.setHistoryBook).toHaveBeenCalledWith(2, "bkold");
  });

  test("a title can be removed from the history", async () => {
    const user = userEvent.setup();
    mocks.history.mockResolvedValue(full);
    mocks.historyEntry.mockResolvedValue({ ...hugo, review: null, isbn: null, marks: [] });
    render(<ReadingHistory onBack={() => {}} onOpen={() => {}} />);
    await user.click(await screen.findByRole("button", { name: /Evelyn Hugo/ }));
    const sheet = screen.getByRole("dialog");
    await user.click(within(sheet).getByRole("button", { name: "Remove from my history" }));
    await user.click(within(sheet).getByRole("button", { name: "Yes, remove" }));
    expect(mocks.forgetHistory).toHaveBeenCalledWith(2);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  test("books dropped with the exports go on the shelf in the same import", async () => {
    const user = userEvent.setup({ applyAccept: false });
    mocks.importHistory.mockResolvedValue({ books_added: 2, books_updated: 0, highlights_added: 0, skipped: 0 });
    mocks.importBook
      .mockResolvedValueOnce({ book_id: "bk1" })
      .mockRejectedValueOnce(new Error("book already imported as 'bk0'"))
      .mockRejectedValueOnce(new Error("This book is copy-protected (DRM), so it can’t be opened here."));
    render(<ReadingHistory onBack={() => {}} onOpen={() => {}} />);
    await user.upload(await screen.findByLabelText("Choose export files"), [
      new File([GOODREADS], "goodreads_library_export.csv"),
      new File(["x"], "one.epub", { type: "application/epub+zip" }),
      new File(["x"], "two.epub", { type: "application/epub+zip" }),
      new File(["x"], "locked.epub", { type: "application/epub+zip" }),
      new File(["x"], "scan.pdf", { type: "application/pdf" }),
    ]);
    const files = within(await screen.findByRole("list", { name: "Chosen files" }));
    expect(files.getAllByText("A book. It goes on your shelf when you import.")).toHaveLength(3);
    expect(files.getByText(/PDF/)).toBeTruthy();
    mocks.history.mockResolvedValue(full);
    await user.click(screen.getByRole("button", { name: "Import 2 books and 3 books for your shelf" }));
    const outcome = await screen.findByRole("status");
    expect(outcome.textContent).toContain("Put 2 books on your shelf.");
    expect(outcome.textContent).toContain("Brought in 2 new books.");
    // the one that could not be added stays listed, with the reason
    const left = within(screen.getByRole("list", { name: "Chosen files" }));
    expect(left.getByText("locked.epub")).toBeTruthy();
    expect(left.getByText(/copy-protected/)).toBeTruthy();
    expect(left.queryByText("one.epub")).toBeNull();
  });

  test("everything imported can be removed, after asking", async () => {
    const user = userEvent.setup();
    mocks.history.mockResolvedValue(full);
    render(<ReadingHistory onBack={() => {}} onOpen={() => {}} />);
    await user.click(await screen.findByRole("button", { name: "Remove everything I imported" }));
    expect(screen.getByText(/Your Mothli books and notes are not touched/)).toBeTruthy();
    expect(mocks.forgetHistory).not.toHaveBeenCalled();
    mocks.history.mockResolvedValue(nothing);
    await user.click(screen.getByRole("button", { name: "Yes, remove it all" }));
    expect(mocks.forgetHistory).toHaveBeenCalledWith();
    expect(await screen.findByRole("heading", { name: "Where have you been reading?" })).toBeTruthy();
  });

  test("a Kobo or Apple Books database is recognised and read on the server with the other files", async () => {
    const user = userEvent.setup({ applyAccept: false });
    const database = new File([new TextEncoder().encode("SQLite format 3\u0000"), new Uint8Array(64)], "KoboReader.sqlite");
    mocks.importHistory.mockResolvedValue({ books_added: 2, books_updated: 0, highlights_added: 0, skipped: 0 });
    mocks.importHistoryDatabases.mockResolvedValue({
      books_added: 40, books_updated: 1, highlights_added: 300, skipped: 0, sources: [{ source: "kobo", books: 41, highlights: 300 }],
    });
    render(<ReadingHistory onBack={() => {}} onOpen={() => {}} />);
    await user.upload(await screen.findByLabelText("Choose export files"), [database, new File([GOODREADS], "g.csv")]);
    expect(await screen.findByText("A Kobo or Apple Books database. It is read when you import.")).toBeTruthy();
    mocks.history.mockResolvedValue(full);
    await user.click(screen.getByRole("button", { name: "Import 2 books and 1 reader database" }));
    const outcome = await screen.findByRole("status");
    expect(outcome.textContent).toContain("Brought in 42 new books and 300 new highlights.");
    expect(outcome.textContent).toContain("Filled in details for 1 book you already had.");
    expect(mocks.importHistoryDatabases).toHaveBeenCalledWith([database]);
  });

  test("a database the server cannot read says why", async () => {
    const user = userEvent.setup({ applyAccept: false });
    const database = new File([new TextEncoder().encode("SQLite format 3\u0000")], "AEAnnotation.sqlite");
    mocks.importHistoryDatabases.mockRejectedValue(new Error("That is the Apple Books highlights file. Bring the library file from the BKLibrary folder with it."));
    render(<ReadingHistory onBack={() => {}} onOpen={() => {}} />);
    await user.upload(await screen.findByLabelText("Choose export files"), database);
    await user.click(await screen.findByRole("button", { name: "Import 1 reader database" }));
    expect((await screen.findByRole("alert")).textContent).toContain("Bring the library file from the BKLibrary folder");
  });

  test("Readwise highlights come with one pasted token, which is cleared afterwards", async () => {
    const user = userEvent.setup();
    mocks.importReadwise.mockResolvedValue({
      books_added: 12, books_updated: 0, highlights_added: 480, skipped: 0, sources: [{ source: "readwise", books: 12, highlights: 480 }],
    });
    render(<ReadingHistory onBack={() => {}} onOpen={() => {}} />);
    await user.click(await screen.findByRole("button", { name: /Readwise/ }));
    const field = screen.getByLabelText("Your Readwise access token") as HTMLInputElement;
    expect(field.type).toBe("password");
    const go = screen.getByRole("button", { name: "Bring from Readwise" }) as HTMLButtonElement;
    expect(go.disabled).toBe(true);
    await user.type(field, "  abcDEF1234567890abcDEF1234567890  ");
    mocks.history.mockResolvedValue(full);
    await user.click(go);
    expect(mocks.importReadwise).toHaveBeenCalledWith("abcDEF1234567890abcDEF1234567890");
    expect((await screen.findByRole("status")).textContent).toContain("Brought in 12 new books and 480 new highlights.");
    expect(field.value).toBe("");
  });

  test("a token Readwise refuses is explained", async () => {
    const user = userEvent.setup();
    mocks.importReadwise.mockRejectedValue(new Error("Readwise didn’t accept that access token. Copy it again from readwise.io/access_token."));
    render(<ReadingHistory onBack={() => {}} onOpen={() => {}} />);
    await user.click(await screen.findByRole("button", { name: /Readwise/ }));
    await user.type(screen.getByLabelText("Your Readwise access token"), "x".repeat(32));
    await user.click(screen.getByRole("button", { name: "Bring from Readwise" }));
    expect((await screen.findByRole("alert")).textContent).toContain("didn’t accept that access token");
  });

  test("greets a returning reader with something they once underlined, which opens its book", async () => {
    const user = userEvent.setup();
    mocks.history.mockResolvedValue(full);
    mocks.historyGlimpse.mockResolvedValue({
      mark: { text: "It is a truth universally acknowledged", entry_id: 1, title: "Pride and Prejudice", author: "Jane Austen" },
    });
    render(<ReadingHistory onBack={() => {}} onOpen={() => {}} />);
    expect(await screen.findByText("Something you once underlined")).toBeTruthy();
    expect(screen.getByText("It is a truth universally acknowledged")).toBeTruthy();
    const greeting = screen.getByText("Something you once underlined").closest("figure") as HTMLElement;
    await user.click(within(greeting).getByRole("button", { name: "Pride and Prejudice, Jane Austen" }));
    expect(await screen.findByRole("dialog", { name: "Pride and Prejudice" })).toBeTruthy();
    expect(mocks.historyEntry).toHaveBeenCalledWith(1);
  });

  test("finished books sit under the year they were finished", async () => {
    const user = userEvent.setup();
    const older = { ...hugo, id: 3, title: "Dracula", author: "Bram Stoker", status: "read" as const, finished_on: "2020-10-31" };
    const undated = { ...hugo, id: 4, title: "Circe", author: "Madeline Miller", status: "read" as const, finished_on: null };
    mocks.history.mockImplementation(async (search: { status?: string } = {}) =>
      search.status === "read" ? { ...full, matches: 3, entries: [austen, older, undated] } : full,
    );
    render(<ReadingHistory onBack={() => {}} onOpen={() => {}} />);
    await screen.findByRole("button", { name: "Pride and Prejudice, Jane Austen" });
    expect(screen.queryByRole("heading", { level: 3, name: "2021" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "1 book read" }));
    expect(await screen.findByRole("heading", { level: 3, name: "2021" })).toBeTruthy();
    expect(screen.getByRole("heading", { level: 3, name: "2020" })).toBeTruthy();
    expect(screen.getByRole("heading", { level: 3, name: "Sometime before" })).toBeTruthy();
    expect(screen.getByRole("button", { name: /Read 1/ }).getAttribute("aria-pressed")).toBe("true");
  });
});
