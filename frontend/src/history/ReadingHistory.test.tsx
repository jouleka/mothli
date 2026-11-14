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
});

describe("Your reading history", () => {
  test("starts with a place to bring files, says what it reads and what it cannot move", async () => {
    const { container } = render(<ReadingHistory onBack={() => {}} onOpen={() => {}} />);
    expect(screen.getByRole("heading", { level: 1, name: "Your reading history" })).toBeTruthy();
    expect(await screen.findByRole("heading", { name: "Bring in your files" })).toBeTruthy();
    expect(screen.getByText("Goodreads · StoryGraph · Kindle highlights · Libby")).toBeTruthy();
    expect(screen.getByText(/copy-protected, so the books themselves can’t be moved/)).toBeTruthy();
    await userEvent.setup().click(screen.getByText("How do I get my files?"));
    expect(screen.getByText(/My Clippings.txt/)).toBeTruthy();
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
    expect(screen.queryByRole("heading", { name: "Bring in your files" })).toBeNull();
    expect(screen.getByRole("button", { name: "Import more" })).toBeTruthy();
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

  test("a book that is not in the free library says how to bring your own copy, and can be removed", async () => {
    const user = userEvent.setup();
    mocks.history.mockResolvedValue(full);
    mocks.historyEntry.mockResolvedValue({ ...hugo, review: null, isbn: null, marks: [] });
    render(<ReadingHistory onBack={() => {}} onOpen={() => {}} />);
    const row = await screen.findByRole("button", { name: "The Seven Husbands of Evelyn Hugo, Taylor Jenkins Reid" });
    await user.click(row);
    const sheet = screen.getByRole("dialog");
    expect(within(sheet).getByText(/isn’t in the free Library/)).toBeTruthy();
    expect(within(sheet).queryByRole("button", { name: "Add it free" })).toBeNull();
    await user.click(within(sheet).getByRole("button", { name: "Remove from my history" }));
    await user.click(within(sheet).getByRole("button", { name: "Yes, remove" }));
    expect(mocks.forgetHistory).toHaveBeenCalledWith(2);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
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
    expect(await screen.findByRole("heading", { name: "Bring in your files" })).toBeTruthy();
  });
});
