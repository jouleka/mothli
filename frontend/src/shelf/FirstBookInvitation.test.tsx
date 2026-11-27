/** A new reader's first book: Mothli's one invitation about it on the shelf, shown once, with an
 * equal way to just keep reading. The plan sheet is a stand-in here (its own tests are
 * PlanSheet.test.tsx). */
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { axeAA } from "../test-a11y";

const mocks = vi.hoisted(() => ({
  books: vi.fn(),
  importBook: vi.fn(),
  classics: vi.fn(),
  plan: vi.fn(),
  homeConversation: vi.fn(),
  owner: vi.fn(),
}));
vi.mock("../api", () => ({
  api: {
    books: mocks.books,
    importBook: mocks.importBook,
    classics: mocks.classics,
    plan: mocks.plan,
    homeConversation: mocks.homeConversation,
  },
  ApiError: class ApiError extends Error {
    status = 0;
  },
}));
vi.mock("../pwa/offline", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../pwa/offline")>()),
  activeOfflineOwner: mocks.owner,
}));
vi.mock("./PlanSheet", async (importOriginal) => {
  const { createElement: h } = await import("react");
  const { createPortal } = await import("react-dom");
  return {
    ...(await importOriginal<typeof import("./PlanSheet")>()),
    PlanSheet: ({ onClose, onJoined }: { onClose: () => void; onJoined?: (plan: unknown) => void }) =>
      createPortal(
        h("div", { role: "dialog", "aria-modal": "true", "aria-label": "The plan" },
          h("button", { type: "button", onClick: () => onJoined?.({ state: "paying" }) }, "Pay"),
          h("button", { type: "button", onClick: onClose }, "Close the sheet")),
        document.body),
  };
});

import { keepQuestion } from "../ui/keptQuestion";
import { type MothliStanding, setMothliStanding } from "../ui/mothliStanding";
import { FIRST_BOOK_AFTER_TITLE, KEEP_READING_FREE } from "./FirstBookInvitation";
import { Shelf } from "./Shelf";

const session = {
  user: { id: "05dbebd1-e9e8-4be8-8172-eb65ca8d2a67", display_name: "Wren Example" },
  expires_at: "2099-01-01T00:00:00+00:00",
  chat: true,
};
const PRIDE = { book_id: "p1", title: "Pride and Prejudice", author: "Jane Austen" };
const invitation = () => document.querySelector<HTMLElement>("section.ml-first-invite");
/* By class rather than by role: a role query over the whole shelf is slow enough to time out. */
const findInvitation = () =>
  waitFor(() => {
    const card = invitation();
    if (!card) throw new Error("no invitation yet");
    return card;
  });
const shelf = () => render(<Shelf onOpen={() => {}} session={session} />);
/** The shelf has settled: its books are listed. */
const settled = () => screen.findByRole("button", { name: "Pride and Prejudice, Jane Austen" });

beforeEach(() => {
  localStorage.clear();
  mocks.owner.mockReturnValue("reader-a");
  mocks.books.mockReset().mockResolvedValue([PRIDE]);
  mocks.importBook.mockReset().mockResolvedValue({});
  mocks.classics.mockReset().mockRejectedValue(new Error("no catalogue"));
  mocks.plan.mockReset().mockRejectedValue(new Error("no plan here"));
  mocks.homeConversation.mockReset().mockResolvedValue({ turns: [] });
  setMothliStanding({ state: "new", why: null });
});
afterEach(() => {
  setMothliStanding(null);
});

describe("the first book's invitation", () => {
  // Catches: the pitch drifting into price, numbers, technology or guilt.
  test("speaks of the reader's book in Mothli's voice, with no price, number or technology", async () => {
    const { container } = shelf();
    const card = await findInvitation();
    // A named region on the shelf, read out by its own words.
    expect(screen.getByRole("region", { name: /^Ooh, Pride and Prejudice\s*!/ })).toBe(card);
    expect(card.querySelector("p")?.textContent).toBe(`Ooh, Pride and Prejudice! ${FIRST_BOOK_AFTER_TITLE}`);
    expect(card.textContent).not.toMatch(/\d|\$|€|£/);
    expect(card.textContent).not.toMatch(/\bAI\b|the chat|model|trial|credits?|unlimited|only|left|hurry/i);
    expect(within(card).getByRole("button", { name: "Talk with Mothli" })).toBeTruthy();
    expect(within(card).getByRole("button", { name: KEEP_READING_FREE })).toBeTruthy();
    // Nothing is blocked: the books and the reading are all still there.
    expect(screen.getByRole("button", { name: "Start reading" })).toBeTruthy();
    expect(await axeAA(container)).toHaveNoViolations();
  });

  // Catches: a reader who said no being asked again, here or next visit.
  test("Keep reading for free closes it for good, and focus carries on to the book", async () => {
    const first = shelf();
    await findInvitation();
    fireEvent.click(screen.getByRole("button", { name: KEEP_READING_FREE }));
    expect(invitation()).toBeNull();
    await waitFor(() => expect(document.activeElement?.textContent).toBe("Start reading"));
    first.unmount();
    shelf();
    await settled();
    expect(invitation()).toBeNull();
  });

  test("Escape closes it too", async () => {
    shelf();
    const card = await findInvitation();
    fireEvent.keyDown(card, { key: "Escape" });
    expect(invitation()).toBeNull();
  });

  // Catches: the card shown again because the reader neither joined nor said no.
  test("it is shown once, even when the reader pressed nothing", async () => {
    const first = shelf();
    await findInvitation();
    first.unmount();
    shelf();
    await settled();
    expect(invitation()).toBeNull();
  });

  test("once is per reader: another reader on this device still sees theirs", async () => {
    const first = shelf();
    await findInvitation();
    first.unmount();
    mocks.owner.mockReturnValue("reader-b");
    shelf();
    expect(await findInvitation()).toBeTruthy();
  });

  test("the first button opens the plan sheet; closing it without joining closes the card", async () => {
    shelf();
    const card = await findInvitation();
    fireEvent.click(within(card).getByRole("button", { name: "Talk with Mothli" }));
    // Escape inside the sheet is the sheet's, not the card's.
    fireEvent.keyDown(await screen.findByRole("dialog", { name: "The plan" }), { key: "Escape" });
    expect(invitation()).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Close the sheet" }));
    await waitFor(() => expect(invitation()).toBeNull());
    expect(screen.queryByRole("dialog", { name: "The plan" })).toBeNull();
  });

  test("joining takes the card away", async () => {
    shelf();
    const card = await findInvitation();
    fireEvent.click(within(card).getByRole("button", { name: "Talk with Mothli" }));
    fireEvent.click(await screen.findByRole("button", { name: "Pay" }));
    fireEvent.click(screen.getByRole("button", { name: "Close the sheet" }));
    await waitFor(() => expect(invitation()).toBeNull());
  });

  // Catches: a sign-up pitch to a reader who has or had Mothli, or where the server told nothing.
  test.each([
    [{ state: "paying", why: null }],
    [{ state: "ended", why: "plan" }],
    [null],
  ] as [MothliStanding | null][])("never for a reader standing %j", async (standing) => {
    setMothliStanding(standing);
    shelf();
    await settled();
    expect(invitation()).toBeNull();
    expect(screen.queryByText(KEEP_READING_FREE)).toBeNull();
  });

  test("never on a server without Mothli's chat", async () => {
    render(<Shelf onOpen={() => {}} session={{ ...session, chat: false }} />);
    await settled();
    expect(invitation()).toBeNull();
  });

  test("not on an empty shelf; it comes with the first book the reader adds", async () => {
    mocks.books.mockResolvedValue([]);
    const { container } = shelf();
    await screen.findByText("Make yourself at home.");
    expect(screen.queryByText(KEEP_READING_FREE)).toBeNull();
    mocks.books.mockResolvedValue([PRIDE]);
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    await act(async () => {
      fireEvent.change(input, { target: { files: [new File([new Uint8Array([1])], "pride.epub", { type: "application/epub+zip" })] } });
    });
    expect(await findInvitation()).toBeTruthy();
  });

  // Catches: the card laid over a question the reader asked anyway and is kept for them.
  test.each([["home"], [{ book: "p1" }]] as const)("not over a kept question (%j), and not used up", async (scope) => {
    keepQuestion(scope, "Who is Mr Darcy?");
    const first = shelf();
    await settled();
    expect(invitation()).toBeNull();
    first.unmount();
    localStorage.removeItem(Object.keys(localStorage).find((key) => key.startsWith("mothli.keptQuestion:"))!);
    shelf();
    expect(await findInvitation()).toBeTruthy();
  });
});
