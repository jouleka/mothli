/** The hosted account sheet: who is signed in, "What Mothli remembers" only where the server
 * keeps it, and a sign-out that says plainly what it clears. */
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { axeAA } from "../test-a11y";

const mocks = vi.hoisted(() => ({
  recollections: vi.fn(), logout: vi.fn(), deleteAccount: vi.fn(), exportAccount: vi.fn(),
}));
vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: mocks,
}));
import { ApiError } from "../api";
import { Account } from "./Account";

const session = {
  user: { id: "05dbebd1-e9e8-4be8-8172-eb65ca8d2a67", display_name: "Wren Example" },
  expires_at: "2099-01-01T00:00:00+00:00",
};
const assign = vi.fn();
const realLocation = window.location;

beforeEach(() => {
  mocks.recollections.mockReset().mockResolvedValue({ recollections: [], hidden: 0 });
  mocks.logout.mockReset().mockResolvedValue(undefined);
  mocks.deleteAccount.mockReset().mockResolvedValue({ erase_after: "2099-12-18T00:00:00+00:00" });
  sessionStorage.clear();
  assign.mockReset();
  Object.defineProperty(window, "location", { configurable: true, value: { ...realLocation, assign } });
});

afterEach(() => {
  Object.defineProperty(window, "location", { configurable: true, value: realLocation });
});

describe("Account", () => {
  test("names the reader and offers what Mothli remembers where the server keeps it", async () => {
    const { baseElement } = render(<Account session={session} onClose={() => {}} />);
    const sheet = screen.getByRole("dialog", { name: "Your account" });
    expect(within(sheet).getByText("Wren Example")).toBeTruthy();
    fireEvent.click(await within(sheet).findByRole("button", { name: "What Mothli remembers" }));
    expect(await screen.findByRole("dialog", { name: "What Mothli remembers" })).toBeTruthy();
    expect(screen.queryByRole("dialog", { name: "Your account" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    const back = await screen.findByRole("dialog", { name: "Your account" });
    await waitFor(() => expect(document.activeElement).toBe(within(back).getByRole("button", { name: "What Mothli remembers" })));
    expect(await axeAA(baseElement)).toHaveNoViolations();
  });

  // Catches: a door to a screen the server does not have (hosted answers 404 until a later part).
  test("does not show what Mothli remembers where the server answers 404", async () => {
    mocks.recollections.mockRejectedValue(new ApiError("Not Found", 404));
    render(<Account session={session} onClose={() => {}} />);
    await waitFor(() => expect(mocks.recollections).toHaveBeenCalled());
    await act(() => new Promise((settled) => setTimeout(settled, 0)));
    expect(screen.queryByRole("button", { name: "What Mothli remembers" })).toBeNull();
    expect(screen.getByRole("button", { name: "Sign out" })).toBeTruthy();
  });

  // Catches: sign-out wording that hides what it clears, and a sign-out that sends the reader
  // straight to the sign-in service instead of back to the welcome.
  test("signing out says what it clears and comes back to the welcome", async () => {
    render(<Account session={session} onClose={() => {}} />);
    const leave = screen.getByRole("button", { name: "Sign out" });
    expect(leave.getAttribute("aria-describedby")).toBeTruthy();
    expect(document.getElementById(leave.getAttribute("aria-describedby")!)?.textContent).toMatch(
      /clears the books you saved for offline reading on this device/,
    );
    fireEvent.click(leave);
    await waitFor(() => expect(assign).toHaveBeenCalledWith("/"));
    expect(mocks.logout).toHaveBeenCalledTimes(1);
  });

  test("a sign-out that fails says so kindly and can be tried again", async () => {
    mocks.logout.mockRejectedValueOnce(new TypeError("offline"));
    render(<Account session={session} onClose={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
    expect((await screen.findByRole("alert")).textContent).toBe(
      "I couldn’t finish signing you out. Check your connection and try again.",
    );
    expect(assign).not.toHaveBeenCalled();
    expect((screen.getByRole("button", { name: "Sign out" }) as HTMLButtonElement).disabled).toBe(false);
  });

  // Catches: a deletion one stray click away, a second step that hides what will happen, or one
  // with no way back.
  test("deleting asks once more, says plainly what happens, and can be backed out of", async () => {
    const { baseElement } = render(<Account session={session} onClose={() => {}} />);
    const door = screen.getByRole("button", { name: "Delete my account" });
    fireEvent.click(door);
    expect(mocks.deleteAccount).not.toHaveBeenCalled();
    const sheet = screen.getByRole("dialog", { name: "Delete your account?" });
    expect(document.activeElement).toBe(within(sheet).getByRole("heading", { name: "Delete your account?" }));
    const steps = within(sheet).getAllByRole("listitem").map((item) => item.textContent?.replace(/\s+/g, " "));
    expect(steps).toEqual([
      "You’re signed out everywhere, on every device, straight away.",
      "Everything is kept for 30 days, in case you change your mind. Sign in again before then and you can keep your account.",
      "After that, your books, your place in them, your conversations and what Mothli remembers are erased for good.",
    ]);
    const confirm = within(sheet).getByRole("button", { name: "Delete my account" }) as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
    fireEvent.click(confirm);
    expect(mocks.deleteAccount).not.toHaveBeenCalled();
    const understood = within(sheet).getByRole("checkbox", {
      name: "I understand that after 30 days this can’t be undone.",
    });
    fireEvent.click(understood);
    expect(confirm.disabled).toBe(false);
    expect(await axeAA(baseElement)).toHaveNoViolations();
    // Tab from the last control comes back round inside the sheet.
    confirm.focus();
    fireEvent.keyDown(document, { key: "Tab" });
    expect(sheet.contains(document.activeElement)).toBe(true);

    fireEvent.click(within(sheet).getByRole("button", { name: "Go back" }));
    expect(screen.getByRole("dialog", { name: "Your account" })).toBeTruthy();
    await waitFor(() => expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "Delete my account" })));
    expect(mocks.deleteAccount).not.toHaveBeenCalled();
    // Coming back asks for the "I understand" again.
    fireEvent.click(screen.getByRole("button", { name: "Delete my account" }));
    expect((screen.getByRole("checkbox") as HTMLInputElement).checked).toBe(false);
  });

  test("deleting signs out and goes back to the welcome, which will name the day", async () => {
    render(<Account session={session} onClose={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: "Delete my account" }));
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Delete my account" }));
    await waitFor(() => expect(assign).toHaveBeenCalledWith("/"));
    expect(mocks.deleteAccount).toHaveBeenCalledTimes(1);
    expect(sessionStorage.getItem("mothli.account-deleted")).toBe("2099-12-18T00:00:00+00:00");
  });

  test("a deletion that fails says so kindly and can be tried again", async () => {
    mocks.deleteAccount.mockRejectedValueOnce(new TypeError("offline"));
    render(<Account session={session} onClose={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: "Delete my account" }));
    fireEvent.click(screen.getByRole("checkbox"));
    const confirm = within(screen.getByRole("dialog")).getByRole("button", { name: "Delete my account" });
    fireEvent.click(confirm);
    expect((await screen.findByRole("alert")).textContent).toBe(
      "I couldn’t do that just now. Check your connection and try again.");
    expect(assign).not.toHaveBeenCalled();
    expect((confirm as HTMLButtonElement).disabled).toBe(false);
  });

  test("Escape closes it", () => {
    const onClose = vi.fn();
    render(<Account session={session} onClose={onClose} />);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

describe("Taking a copy of my data", () => {
  const saved: { name: string; href: string }[] = [];
  const realCreate = URL.createObjectURL;
  const realRevoke = URL.revokeObjectURL;
  beforeEach(() => {
    saved.length = 0;
    mocks.exportAccount.mockReset().mockResolvedValue({
      blob: new Blob(["zip"], { type: "application/zip" }), name: "mothli-my-data-2026-11-17.zip" });
    URL.createObjectURL = vi.fn(() => "blob:made-up");
    URL.revokeObjectURL = vi.fn();
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      saved.push({ name: this.download, href: this.href });
    });
  });
  afterEach(() => {
    URL.createObjectURL = realCreate;
    URL.revokeObjectURL = realRevoke;
    vi.restoreAllMocks();
  });

  // Catches: no way to take one's data out; a door that does not say what is in it or that the
  // book files are not; a copy that never reaches the reader's downloads.
  test("a quiet door says what is in it, and the copy is saved", async () => {
    const { baseElement } = render(<Account session={session} onClose={() => {}} />);
    const sheet = screen.getByRole("dialog", { name: "Your account" });
    const door = within(sheet).getByRole("button", { name: "Take a copy of my data" });
    expect(document.getElementById(door.getAttribute("aria-describedby")!)?.textContent).toBe(
      "Your shelf, your place in each book, your highlights and notes, your chats and what Mothli remembers, in one file. The books themselves aren’t in it.");
    fireEvent.click(door);
    expect(within(sheet).getByRole("status").textContent).toBe("Getting your copy ready…");
    expect((door as HTMLButtonElement).disabled).toBe(true);
    await waitFor(() => expect(within(sheet).getByRole("status").textContent).toBe(
      "Done. Your copy is in your downloads."));
    expect(mocks.exportAccount).toHaveBeenCalledTimes(1);
    expect(saved).toEqual([{ name: "mothli-my-data-2026-11-17.zip", href: "blob:made-up" }]);
    expect((door as HTMLButtonElement).disabled).toBe(false);
    expect(await axeAA(baseElement)).toHaveNoViolations();
  });

  // Catches: a refusal shown as a raw error, or a "try again" line where waiting is the answer.
  test("too soon says the server's kind line; anything else says to try again", async () => {
    mocks.exportAccount.mockRejectedValueOnce(new ApiError(
      "You just took a copy. Give it a few minutes, then try again.", 429, "export_too_soon"));
    render(<Account session={session} onClose={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: "Take a copy of my data" }));
    expect((await screen.findByRole("alert")).textContent).toBe(
      "You just took a copy. Give it a few minutes, then try again.");
    mocks.exportAccount.mockRejectedValueOnce(new TypeError("offline"));
    fireEvent.click(screen.getByRole("button", { name: "Take a copy of my data" }));
    await waitFor(() => expect(screen.getByRole("alert").textContent).toBe(
      "I couldn’t make your copy just now. Check your connection and try again."));
    expect(saved).toEqual([]);
  });

  // Catches: a reader deleting their account without being offered their data first.
  test("the delete confirmation offers it before they go", async () => {
    render(<Account session={session} onClose={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: "Delete my account" }));
    const sheet = screen.getByRole("dialog", { name: "Delete your account?" });
    const door = within(sheet).getByRole("button", { name: "Take a copy of my data" });
    expect(document.getElementById(door.getAttribute("aria-describedby")!)?.textContent).toBe(
      "Before you go, you can keep a copy of your shelf, your notes and your chats. The books themselves aren’t in it.");
    fireEvent.click(door);
    await waitFor(() => expect(saved).toHaveLength(1));
    expect(mocks.deleteAccount).not.toHaveBeenCalled();
  });
});
