import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, test, vi } from "vitest";
import { axeAA } from "../test-a11y";

const mocks = vi.hoisted(() => ({
  homeConversation: vi.fn(), askHome: vi.fn(), clearHome: vi.fn(), readChapters: vi.fn(), addClassic: vi.fn(),
  recollections: vi.fn(), forgetRecollection: vi.fn(), forgetRecollections: vi.fn(),
}));
vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: mocks,
}));
import { ApiError, type Recollection } from "../api";
import { HomeChat } from "./HomeChat";

const night: Recollection = {
  id: 3, kind: "about", text: "The reader reads at night, on the train home.", said_at: "2026-11-02T12:00:00+00:00",
  book: null, message: "I always read at night, on the train home.",
};
const wrecked: Recollection = {
  id: 2, kind: "feeling", text: "Part 1 wrecked the reader: Mara and that lamp.", said_at: "2026-10-30T08:00:00+00:00",
  book: { book_id: "bk1", title: "The Lamp Book" }, message: null,
};
const short: Recollection = {
  id: 1, kind: "about", text: "The reader wants short answers.", said_at: "2026-10-01T08:00:00+00:00",
  book: null, message: "Short answers please.",
};

beforeEach(() => {
  mocks.homeConversation.mockReset().mockResolvedValue({ turns: [] });
  mocks.recollections.mockReset().mockResolvedValue({ recollections: [night, wrecked, short], hidden: 0 });
  mocks.forgetRecollection.mockReset().mockResolvedValue(undefined);
  mocks.forgetRecollections.mockReset().mockResolvedValue(undefined);
});

const link = () => screen.findByRole("button", { name: "What Mothli remembers" });
async function open() {
  fireEvent.click(await link());
  return screen.findByRole("dialog", { name: "What Mothli remembers" });
}
const rows = (panel: HTMLElement) => within(panel).queryAllByRole("listitem");

describe("What Mothli remembers", () => {
  // Catches: the list not reachable from the home chat; a note shown without its book, its date
  // or the reader's own words; the order changed from what the server sends (newest first).
  test("is reached from the home chat and lists what the server returns, newest first", async () => {
    const { baseElement } = render(<HomeChat />);
    const panel = await open();
    expect(mocks.recollections).toHaveBeenCalledTimes(1);
    await within(panel).findByText(night.text);
    expect(rows(panel).map((row) => row.querySelector(".ml-mem-text")?.textContent)).toEqual(
      [night.text, wrecked.text, short.text]);
    const first = rows(panel)[0];
    expect(within(first).getByText(/2 November 2026/)).toBeTruthy();
    expect(within(first).getByText("“I always read at night, on the train home.”")).toBeTruthy();
    expect(within(rows(panel)[1]).getByText("About The Lamp Book")).toBeTruthy();
    expect(rows(panel)[1].querySelector("blockquote")).toBeNull();     // no words of the reader's to show
    expect(within(panel).getByRole("heading", { name: "What Mothli remembers" })).toBeTruthy();
    // Words for a reader, not a programmer.
    expect(panel.textContent).not.toMatch(/recollection|distil|note id/i);
    expect(await axeAA(baseElement)).toHaveNoViolations();
  });

  // Catches: "Forget this" that only hides the row, or deletes the wrong one; focus lost to the page.
  test("“Forget this” forgets that one, takes the row away and moves focus to the next", async () => {
    render(<HomeChat />);
    const panel = await open();
    await within(panel).findByText(wrecked.text);
    const forget = within(rows(panel)[1]).getByRole("button", { name: "Forget this" });
    expect(forget.getAttribute("aria-describedby")).toBeTruthy();
    await act(async () => { fireEvent.click(forget); });
    expect(mocks.forgetRecollection).toHaveBeenCalledWith(2);
    expect(within(panel).queryByText(wrecked.text)).toBeNull();
    expect(rows(panel)).toHaveLength(2);
    expect(document.activeElement).toBe(within(rows(panel)[1]).getByRole("button", { name: "Forget this" }));
    // The last one goes: focus moves to the one before it.
    await act(async () => { fireEvent.click(within(rows(panel)[1]).getByRole("button", { name: "Forget this" })); });
    expect(document.activeElement).toBe(within(rows(panel)[0]).getByRole("button", { name: "Forget this" }));
  });

  // Catches: a failed delete that removes the row anyway, or says nothing.
  test("a failed “Forget this” says so and keeps the row", async () => {
    mocks.forgetRecollection.mockRejectedValueOnce(new ApiError("nope", 500));
    render(<HomeChat />);
    const panel = await open();
    await within(panel).findByText(night.text);
    await act(async () => { fireEvent.click(within(rows(panel)[0]).getByRole("button", { name: "Forget this" })); });
    expect((await within(panel).findByRole("alert")).textContent).toMatch(/couldn’t forget that/i);
    expect(within(panel).getByText(night.text)).toBeTruthy();
    expect(rows(panel)).toHaveLength(3);
  });

  // Catches: one press wiping everything; a confirmation that does not say what happens.
  test("“Forget everything” asks first, in plain words, and can be called off", async () => {
    mocks.recollections.mockResolvedValue({ recollections: [night], hidden: 2 });
    render(<HomeChat />);
    const panel = await open();
    await within(panel).findByText(night.text);
    fireEvent.click(within(panel).getByRole("button", { name: "Forget everything" }));
    expect(mocks.forgetRecollections).not.toHaveBeenCalled();
    const ask = within(panel).getByRole("group", { name: /Forget everything Mothli remembers about you\?/ });
    expect(ask.textContent).toMatch(/kept back/);
    expect(ask.textContent).toMatch(/can’t be undone/);
    fireEvent.click(within(ask).getByRole("button", { name: "Keep them" }));
    expect(mocks.forgetRecollections).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(within(panel).getByRole("button", { name: "Forget everything" }));
    fireEvent.click(within(panel).getByRole("button", { name: "Forget everything" }));
    await act(async () => {
      fireEvent.click(within(panel).getByRole("button", { name: "Yes, forget everything" }));
    });
    expect(mocks.forgetRecollections).toHaveBeenCalledTimes(1);
    expect(rows(panel)).toHaveLength(0);
    expect(within(panel).getByText(/hasn’t kept anything about you yet/)).toBeTruthy();
    expect(within(panel).queryByText(/kept back until/)).toBeNull();
  });

  // Catches: the held-back count never mentioned, so the reader thinks the list is everything.
  test("says how many more are kept back until those pages are read again", async () => {
    mocks.recollections.mockResolvedValue({ recollections: [night], hidden: 2 });
    render(<HomeChat />);
    const panel = await open();
    expect(await within(panel).findByText("2 more are kept back until you have read those pages again.")).toBeTruthy();
  });

  // Catches: a blank panel when nothing is kept.
  test("an empty list says Mothli has kept nothing yet and what kind of thing it keeps", async () => {
    mocks.recollections.mockResolvedValue({ recollections: [], hidden: 0 });
    const { baseElement } = render(<HomeChat />);
    const panel = await open();
    const empty = await within(panel).findByText(/hasn’t kept anything about you yet/);
    expect(empty.textContent).toMatch(/how you like to read/);
    expect(within(panel).queryByRole("button", { name: "Forget everything" })).toBeNull();
    expect(await axeAA(baseElement)).toHaveNoViolations();
  });

  // Catches: a failed load shown as an empty list ("Mothli remembers nothing"), which is not true.
  test("a failed load says so in plain words", async () => {
    mocks.recollections.mockRejectedValue(new ApiError("boom", 500));
    render(<HomeChat />);
    const panel = await open();
    expect((await within(panel).findByRole("alert")).textContent).toMatch(/couldn’t look up what it remembers/i);
    expect(within(panel).queryByText(/hasn’t kept anything/)).toBeNull();
    expect(within(panel).queryByRole("button", { name: "Forget everything" })).toBeNull();
  });

  // Catches: a panel that cannot be left by keyboard, or that drops focus on the page when it closes.
  test("closes with Escape or the close button, and focus goes back to the link", async () => {
    render(<HomeChat />);
    let panel = await open();
    await within(panel).findByText(night.text);
    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(document.activeElement).toBe(await link());
    panel = await open();
    fireEvent.click(within(panel).getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(document.activeElement).toBe(await link());
  });
});
