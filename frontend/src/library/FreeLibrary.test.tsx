import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, test, vi } from "vitest";
import { axeAA } from "../test-a11y";
import { FreeLibrary } from "./FreeLibrary";

const mocks = vi.hoisted(() => ({
  classics: vi.fn(),
  classicsFront: vi.fn(),
  classic: vi.fn(),
  addClassic: vi.fn(),
  warmCovers: vi.fn(),
}));
vi.mock("../api", () => ({ api: mocks }));

type Row = { id: string; title: string; author: string; language: string; book_id: string | null };
const emma: Row = { id: "158", title: "Emma", author: "Jane Austen", language: "en", book_id: null };
const dracula: Row = { id: "345", title: "Dracula", author: "Bram Stoker", language: "en", book_id: null };
const odyssey: Row = { id: "1727", title: "The Odyssey", author: "Homer", language: "en", book_id: "bkodyssey" };
const carmilla: Row = { id: "10007", title: "Carmilla", author: "Joseph Sheridan Le Fanu", language: "en", book_id: null };
const fantine: Row = { id: "17489", title: "Fantine", author: "Victor Hugo", language: "fr", book_id: null };

const summary = {
  enabled: true,
  source: "Project Gutenberg",
  source_url: "https://www.gutenberg.org/",
  total: 77713,
  matches: 77713,
  languages: [
    { code: "en", count: 61513 },
    { code: "fr", count: 4152 },
  ],
  genres: [
    { id: "romance", label: "Romance" },
    { id: "horror", label: "Horror & gothic" },
  ],
  moods: [
    { id: "laugh", label: "Make me laugh" },
    { id: "chills", label: "Keep me up at night" },
  ],
  books: [emma],
};
const page = (books: Row[], matches = books.length) => ({ ...summary, matches, books });
const front = {
  shelves: [
    { kind: "popular", id: "popular", title: "Most read right now", books: [emma, odyssey] },
    { kind: "mood", id: "chills", title: "Keep me up at night", books: [dracula] },
  ],
};
const detail = (book: Row) => ({
  ...book,
  genres: [{ id: "horror", label: "Horror & gothic" }],
  minutes: 420,
  blurb: "A count leaves his castle.",
  source_url: `https://www.gutenberg.org/ebooks/${book.id}`,
});

beforeEach(() => {
  history.replaceState(null, "", "/?library=1");
  mocks.classicsFront.mockReset().mockResolvedValue(front);
  mocks.classic.mockReset().mockImplementation(async (id: string) => detail(id === "345" ? dracula : emma));
  mocks.addClassic.mockReset();
  mocks.warmCovers.mockReset().mockResolvedValue({ started: 0 });
  mocks.classics.mockReset().mockImplementation(async (search: { mood?: string; q?: string; offset?: number } = {}) => {
    if (search.q) return page([]);
    if (search.mood === "chills") return search.offset ? page([carmilla], 2) : page([dracula], 2);
    return summary;
  });
});

describe("The Library", () => {
  test("opens on shelves of books with their cover art, and has no accessibility violations", async () => {
    const { container } = render(<FreeLibrary onBack={() => {}} onOpen={() => {}} />);
    expect(screen.getByRole("heading", { level: 1, name: "The Library" })).toBeTruthy();
    const popular = await screen.findByRole("region", { name: "Most read right now" });
    expect(within(popular).getByRole("button", { name: "Emma, Jane Austen" })).toBeTruthy();
    expect(within(popular).getByText("On your shelf")).toBeTruthy();
    expect(container.querySelector('.fl-card img[src="/api/classics/158/cover"]')).toBeTruthy();
    expect(await screen.findByText(/77,713 books, all out of copyright/)).toBeTruthy();
    expect(screen.getByRole("link", { name: "Project Gutenberg" }).getAttribute("href")).toBe("https://www.gutenberg.org/");
    expect(screen.getByText(/if you live elsewhere, check your country’s rules/)).toBeTruthy();
    expect(await axeAA(container)).toHaveNoViolations();
  });

  test("a mood narrows the library to a grid that keeps loading, and remembers itself in the address", async () => {
    const user = userEvent.setup();
    render(<FreeLibrary onBack={() => {}} onOpen={() => {}} />);
    const mood = await screen.findByRole("button", { name: "Keep me up at night" });
    await user.click(mood);
    expect(mood.getAttribute("aria-pressed")).toBe("true");
    expect(await screen.findByText("2 books")).toBeTruthy();
    const chills = { q: "", language: "", genre: "", mood: "chills", limit: 60 };
    expect(mocks.classics).toHaveBeenCalledWith(chills);
    // the next page is fetched ahead, and the server is told which covers are about to be shown
    await waitFor(() => expect(mocks.classics).toHaveBeenLastCalledWith({ ...chills, offset: 1 }));
    await waitFor(() => expect(mocks.warmCovers).toHaveBeenCalledWith(["10007"]));
    expect(mocks.warmCovers).toHaveBeenCalledWith(["345"]);
    expect(mocks.warmCovers).toHaveBeenCalledWith(["158", "1727", "345"]);
    expect(window.location.search).toBe("?library=1&feel=chills");
    expect(screen.getByRole("heading", { level: 2, name: "Keep me up at night" })).toBeTruthy();
    expect(screen.queryByRole("region", { name: "Most read right now" })).toBeNull();

    const asked = mocks.classics.mock.calls.length;
    await user.click(screen.getByRole("button", { name: "Show more" }));
    // already fetched: the next page appears without another request
    expect(screen.getByRole("button", { name: "Carmilla, Joseph Sheridan Le Fanu" })).toBeTruthy();
    expect(mocks.classics.mock.calls.length).toBe(asked);
    expect(screen.queryByRole("button", { name: "Show more" })).toBeNull();

    await user.click(screen.getByRole("button", { name: "Back to the shelves" }));
    expect(screen.getByRole("region", { name: "Most read right now" })).toBeTruthy();
    expect(window.location.search).toBe("?library=1");

    // going back to a filter already seen shows it at once, both pages, asking for nothing
    await user.click(screen.getByRole("button", { name: "Keep me up at night" }));
    expect(screen.getByRole("button", { name: "Carmilla, Joseph Sheridan Le Fanu" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Dracula, Bram Stoker" })).toBeTruthy();
    expect(mocks.classics.mock.calls.length).toBe(asked);
    expect(mocks.classicsFront).toHaveBeenCalledTimes(1);
  });

  test("See all on a shelf, a genre and a language each filter the catalogue", async () => {
    const user = userEvent.setup();
    mocks.classics.mockImplementation(async (search: { genre?: string; language?: string } = {}) =>
      search.genre === "romance" && search.language === "fr" ? page([fantine]) : search.genre ? page([emma]) : summary,
    );
    render(<FreeLibrary onBack={() => {}} onOpen={() => {}} />);
    await user.click(await screen.findByRole("button", { name: "See all: Keep me up at night" }));
    expect(screen.getByRole("button", { name: "Keep me up at night" }).getAttribute("aria-pressed")).toBe("true");
    await user.click(screen.getByRole("button", { name: "Keep me up at night" }));
    await user.click(screen.getByRole("button", { name: "Romance" }));
    expect(await screen.findByRole("button", { name: "Emma, Jane Austen" })).toBeTruthy();
    await user.selectOptions(screen.getByRole("combobox", { name: "Language" }), "French");
    expect(await screen.findByRole("button", { name: "Fantine, Victor Hugo" })).toBeTruthy();
    expect(window.location.search).toBe("?library=1&lang=fr&kind=romance");
  });

  test("searching says so when nothing matches", async () => {
    const user = userEvent.setup();
    render(<FreeLibrary onBack={() => {}} onOpen={() => {}} />);
    await user.type(screen.getByRole("searchbox", { name: "Search the library" }), "zzz");
    expect(await screen.findByText(/I couldn’t find that one/)).toBeTruthy();
    expect(screen.getByText("0 books")).toBeTruthy();
  });

  test("a book opens up close with its description, is added, and can be read straight away", async () => {
    const user = userEvent.setup();
    const onOpen = vi.fn();
    mocks.addClassic.mockResolvedValue({ book_id: "bkdracula" });
    render(<FreeLibrary onBack={() => {}} onOpen={onOpen} />);
    const card = await screen.findByRole("button", { name: "Dracula, Bram Stoker" });
    await user.click(card);
    const sheet = screen.getByRole("dialog", { name: "Dracula" });
    expect(await within(sheet).findByText("A count leaves his castle.")).toBeTruthy();
    expect(within(sheet).getByText("About 7 hours · Horror & gothic")).toBeTruthy();
    expect(within(sheet).getByRole("link", { name: "From Project Gutenberg" }).getAttribute("href")).toBe(
      "https://www.gutenberg.org/ebooks/345",
    );
    await user.click(within(sheet).getByRole("button", { name: "Add to my books" }));
    expect(mocks.addClassic).toHaveBeenCalledWith("345");
    await user.click(await within(sheet).findByRole("button", { name: "Start reading" }));
    expect(onOpen).toHaveBeenCalledWith("bkdracula");
  });

  test("a failed download is explained, and Escape closes the book and returns focus", async () => {
    const user = userEvent.setup();
    mocks.addClassic.mockRejectedValue(new Error("Couldn’t reach the book’s source. Check your connection and try again."));
    render(<FreeLibrary onBack={() => {}} onOpen={() => {}} />);
    const card = await screen.findByRole("button", { name: "Dracula, Bram Stoker" });
    await user.click(card);
    await user.click(screen.getByRole("button", { name: "Add to my books" }));
    expect((await screen.findByRole("alert")).textContent).toContain("Couldn’t reach");
    expect((screen.getByRole("button", { name: "Add to my books" }) as HTMLButtonElement).disabled).toBe(false);
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(card);
    expect(document.body.style.overflow).toBe("");
  });

  test("a book already on the shelf opens without downloading again, and Your books goes back", async () => {
    const user = userEvent.setup();
    const onOpen = vi.fn();
    const onBack = vi.fn();
    render(<FreeLibrary onBack={onBack} onOpen={onOpen} />);
    await user.click(await screen.findByRole("button", { name: "The Odyssey, Homer" }));
    await user.click(screen.getByRole("button", { name: "Start reading" }));
    expect(onOpen).toHaveBeenCalledWith("bkodyssey");
    expect(mocks.addClassic).not.toHaveBeenCalled();
    await user.keyboard("{Escape}");
    await user.click(screen.getByRole("button", { name: "Your books" }));
    expect(onBack).toHaveBeenCalled();
  });

  test("says when the library cannot be reached, and tries again", async () => {
    const user = userEvent.setup();
    mocks.classicsFront.mockRejectedValueOnce(new Error("offline"));
    render(<FreeLibrary onBack={() => {}} onOpen={() => {}} />);
    expect((await screen.findByRole("alert")).textContent).toContain("Couldn’t reach the library");
    await user.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
    expect(await screen.findByRole("region", { name: "Most read right now" })).toBeTruthy();
  });

  test("the mood and genre rows slide with the mouse wheel and by pulling, without picking a chip", async () => {
    render(<FreeLibrary onBack={() => {}} onOpen={() => {}} />);
    const chip = await screen.findByRole("button", { name: "Keep me up at night" });
    const row = chip.closest("ul")!;
    Object.defineProperty(row, "scrollWidth", { configurable: true, value: 900 });
    Object.defineProperty(row, "clientWidth", { configurable: true, value: 300 });

    // rolling down slides the row and keeps the page still; at the start, rolling up is left to the page
    const down = new WheelEvent("wheel", { deltaY: 120, bubbles: true, cancelable: true });
    row.dispatchEvent(down);
    expect(row.scrollLeft).toBe(120);
    expect(down.defaultPrevented).toBe(true);
    row.scrollLeft = 0;
    const up = new WheelEvent("wheel", { deltaY: -120, bubbles: true, cancelable: true });
    row.dispatchEvent(up);
    expect(up.defaultPrevented).toBe(false);
    // a sideways gesture is the browser's own business
    const sideways = new WheelEvent("wheel", { deltaX: 80, deltaY: 5, bubbles: true, cancelable: true });
    row.dispatchEvent(sideways);
    expect(sideways.defaultPrevented).toBe(false);

    const pointer = (type: string, clientX: number) => {
      const event = new MouseEvent(type, { clientX, button: 0, bubbles: true, cancelable: true });
      Object.defineProperties(event, { pointerType: { value: "mouse" }, pointerId: { value: 1 } });
      chip.dispatchEvent(event);
    };
    pointer("pointerdown", 250);
    pointer("pointermove", 100);
    expect(row.scrollLeft).toBe(150);
    expect(row.dataset.pulling).toBe("true");
    pointer("pointerup", 100);
    expect(row.dataset.pulling).toBeUndefined();
    fireEvent.click(chip);
    expect(chip.getAttribute("aria-pressed")).toBe("false");

    // a press that does not move is still a click
    pointer("pointerdown", 100);
    pointer("pointerup", 100);
    fireEvent.click(chip);
    expect(chip.getAttribute("aria-pressed")).toBe("true");
  });
});
