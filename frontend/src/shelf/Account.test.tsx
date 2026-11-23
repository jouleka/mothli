/** The hosted account sheet: who is signed in, "What Mothli remembers" only where the server
 * keeps it, and a sign-out that says plainly what it clears. */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { axeAA } from "../test-a11y";

const mocks = vi.hoisted(() => ({
  recollections: vi.fn(), logout: vi.fn(), deleteAccount: vi.fn(), exportAccount: vi.fn(),
  plan: vi.fn(), cancelPlan: vi.fn(), keepPlan: vi.fn(), planPortal: vi.fn(), startCheckout: vi.fn(),
}));
vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: mocks,
}));
import { ApiError, type Plan } from "../api";
import { Account } from "./Account";
import { forgetPaddle } from "./paddle";
import { fakePaddle, noCounter, paddleScripts, planOf, settleScript } from "./plan-fakes";
import { DELETING_PLAN, deletingPlanLine, planDay } from "./YourPlan";

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
  // A new reader on a server that cannot sell the plan: one line, no buttons.
  mocks.plan.mockReset().mockResolvedValue(planOf({ state: "new", next_date: null, manage: false, checkout: null }));
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

  // Catches: asking for what Mothli remembers on a server that said it has no chat, where
  // nothing can be remembered and the question can only fail.
  test("on a server without the chat, neither asks nor offers what Mothli remembers", async () => {
    render(<Account session={{ ...session, chat: false }} onClose={() => {}} />);
    await act(() => new Promise((settled) => setTimeout(settled, 0)));
    expect(mocks.recollections).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "What Mothli remembers" })).toBeNull();
    expect(screen.getByRole("button", { name: "Sign out" })).toBeTruthy();
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
    // The copy's own part (the plan's part has a status line of its own while it loads).
    const copy = door.parentElement!;
    expect(document.getElementById(door.getAttribute("aria-describedby")!)?.textContent).toBe(
      "Your shelf, your place in each book, your highlights and notes, your chats and what Mothli remembers, in one file. The books themselves aren’t in it.");
    fireEvent.click(door);
    expect(within(copy).getByRole("status").textContent).toBe("Getting your copy ready…");
    expect((door as HTMLButtonElement).disabled).toBe(true);
    await waitFor(() => expect(within(copy).getByRole("status").textContent).toBe(
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

describe("Your plan", () => {
  const DATE = "2099-12-03T10:00:00+00:00";
  const day = planDay(DATE);
  const tab = { opener: {} as unknown, location: { href: "" }, close: vi.fn() };
  const realOpen = window.open;
  beforeEach(() => {
    forgetPaddle();
    mocks.cancelPlan.mockReset();
    mocks.keepPlan.mockReset();
    mocks.startCheckout.mockReset();
    mocks.planPortal.mockReset().mockResolvedValue({ url: "https://customer-portal.paddle.com/standin" });
    tab.opener = {};
    tab.location.href = "";
    tab.close.mockReset();
    window.open = vi.fn(() => tab as unknown as Window);
  });
  afterEach(() => {
    window.open = realOpen;
    forgetPaddle();
  });

  async function sheetFor(plan: Plan) {
    mocks.plan.mockResolvedValue(plan);
    const view = render(
      <Account session={{ ...session, user: { ...session.user, email: "wren@example.com" } }} onClose={() => {}} />);
    const sheet = screen.getByRole("dialog", { name: "Your account" });
    await waitFor(() => expect(within(sheet).queryByText("Looking up your plan…")).toBeNull());
    return { ...view, sheet, part: sheet.querySelector(".ml-account-plan") as HTMLElement };
  }
  const lines = (part: HTMLElement) => Array.from(part.querySelectorAll("p")).map((line) => line.textContent);
  const buttons = (part: HTMLElement) => within(part).queryAllByRole("button").map((button) => button.textContent);

  const PAYING = { state: "paying", subscribed: true, next_date: DATE } as const;
  // Every state of ADR 0045 section 6, each line exactly, and only the buttons that work.
  test.each<[string, Partial<Plan>, string[], string[]]>([
    ["new", { state: "new", next_date: null },
      ["Talking with Mothli is free for your first two weeks, starting the first time you use it."],
      ["See the plan"]],
    ["free weeks", { state: "free", next_date: DATE }, [`Your free weeks end on ${day}.`], ["See the plan"]],
    ["ended: two weeks", { state: "ended", why: "time", next_date: null },
      ["Your two free weeks with Mothli are over. Your books stay free, always."], ["See the plan"]],
    ["ended: free start used", { state: "ended", why: "ceiling", next_date: null },
      ["Your free start with Mothli is all used up. Your books stay free, always."], ["See the plan"]],
    ["ended: plan", { state: "ended", why: "plan", next_date: null },
      ["Your plan has ended. Your books stay free, always."], ["See the plan"]],
    ["monthly", { ...PAYING, plan: "monthly" }, [`You’re on the monthly plan. It renews on ${day}.`],
      ["Update payment details", "Cancel my plan"]],
    ["yearly", { ...PAYING, plan: "yearly" }, [`You’re on the yearly plan. It renews on ${day}.`],
      ["Update payment details", "Cancel my plan"]],
    ["cancelled", { ...PAYING, plan: "yearly", cancel_scheduled: true }, [`Your plan ends on ${day}.`],
      ["Keep my plan"]],
    ["payment retrying", { ...PAYING, plan: "monthly", payment_retrying: true },
      ["Your last payment didn’t go through. Paddle will try again, or you can update your card here."],
      ["Update payment details", "Cancel my plan"]],
    ["gift", { state: "paying", plan: "gift", next_date: null }, ["Mothli is on the house for you. Enjoy!"], []],
    ["gift with a date", { state: "paying", plan: "gift", next_date: DATE }, ["Mothli is on the house for you. Enjoy!"], []],
    ["gift and a plan", { ...PAYING, plan: "gift" },
      ["Mothli is on the house for you. Enjoy!", "You have a plan of your own too."],
      ["Update payment details", "Cancel my plan"]],
    ["gift and a cancelled plan", { ...PAYING, plan: "gift", cancel_scheduled: true },
      ["Mothli is on the house for you. Enjoy!", "Your plan won’t renew."], ["Keep my plan"]],
  ])("%s", async (_name, values, expected, offered) => {
    const { part } = await sheetFor(planOf({ why: null, plan: null, ...values }));
    expect(lines(part)).toEqual(expected);
    expect(buttons(part)).toEqual(offered);
    noCounter(part);
  });

  test("the plan part passes the accessibility checks", async () => {
    const { baseElement } = await sheetFor(planOf({ ...PAYING, plan: "yearly" }));
    expect(await axeAA(baseElement)).toHaveNoViolations();
  });

  // Catches: a "See the plan" or a cancel button on a server without the routes behind it.
  test("a server without Paddle shows where the reader stands and no button", async () => {
    for (const values of [{ state: "free" as const }, { ...PAYING, plan: "monthly" as const },
      { ...PAYING, plan: "monthly" as const, cancel_scheduled: true }]) {
      const { part, unmount } = await sheetFor(planOf({ ...values, manage: false, checkout: null }));
      expect(lines(part)).toHaveLength(1);
      expect(buttons(part)).toEqual([]);
      unmount();
    }
  });

  test("a server that keeps no plan shows no plan part; one that fails says so", async () => {
    mocks.plan.mockRejectedValueOnce(new ApiError("Not Found", 404));
    const { unmount } = render(<Account session={session} onClose={() => {}} />);
    await waitFor(() => expect(mocks.plan).toHaveBeenCalled());
    await act(() => new Promise((settled) => setTimeout(settled, 0)));
    expect(document.querySelector(".ml-account-plan")).toBeNull();
    unmount();
    mocks.plan.mockRejectedValueOnce(new TypeError("offline"));
    render(<Account session={session} onClose={() => {}} />);
    expect(await screen.findByText("I couldn’t look up your plan just now. Try again in a moment.")).toBeTruthy();
  });

  // Catches: a cancel one click away, a confirmation without the day or the promise, no way to
  // back out, or no way to undo it after.
  test("cancelling asks once, then the plan ends on its day, and can be kept", async () => {
    const monthly = planOf({ ...PAYING, plan: "monthly" });
    const { part, baseElement } = await sheetFor(monthly);
    fireEvent.click(within(part).getByRole("button", { name: "Cancel my plan" }));
    expect(mocks.cancelPlan).not.toHaveBeenCalled();
    const question = within(part).getByText(
      `Cancel your plan? I’ll stay with you until ${day}, and you won’t be charged again. Your books stay yours, always.`);
    await waitFor(() => expect(document.activeElement).toBe(question));
    expect(buttons(part)).toEqual(["Keep my plan", "Cancel my plan"]);
    expect(await axeAA(baseElement)).toHaveNoViolations();
    fireEvent.click(within(part).getByRole("button", { name: "Keep my plan" }));
    await waitFor(() => expect(document.activeElement).toBe(within(part).getByRole("button", { name: "Cancel my plan" })));
    expect(mocks.cancelPlan).not.toHaveBeenCalled();

    mocks.cancelPlan.mockResolvedValueOnce({ ...monthly, cancel_scheduled: true });
    fireEvent.click(within(part).getByRole("button", { name: "Cancel my plan" }));
    fireEvent.click(within(part).getByRole("button", { name: "Cancel my plan" }));
    const ends = await within(part).findByText(`Your plan ends on ${day}.`);
    await waitFor(() => expect(document.activeElement).toBe(ends));
    expect(mocks.cancelPlan).toHaveBeenCalledTimes(1);
    expect(buttons(part)).toEqual(["Keep my plan"]);

    mocks.keepPlan.mockResolvedValueOnce(monthly);
    fireEvent.click(within(part).getByRole("button", { name: "Keep my plan" }));
    expect(await within(part).findByText(`You’re on the monthly plan. It renews on ${day}.`)).toBeTruthy();
    expect(mocks.keepPlan).toHaveBeenCalledTimes(1);
    noCounter(part);
  });

  test("cancelling a plan under a gift names no day it cannot know", async () => {
    const { part } = await sheetFor(planOf({ ...PAYING, plan: "gift" }));
    fireEvent.click(within(part).getByRole("button", { name: "Cancel my plan" }));
    expect(within(part).getByText(
      "Cancel your plan? You won’t be charged again. Your books stay yours, always.")).toBeTruthy();
  });

  // Catches: a refusal that changes the screen, or a raw error.
  test.each([
    ["try_again_soon", 409, "Your plan is renewing right now. Please try again in a few minutes."],
    ["paddle_unavailable", 503, "I can’t reach our payment service just now, so nothing has changed. Please try again in a minute."],
    [null, 0, "I couldn’t do that just now. Check your connection and try again."],
  ])("a refused change (%s) has its kind line and changes nothing", async (code, status, line) => {
    const refused = () => code ? new ApiError("server words", status, code) : new TypeError("offline");
    const monthly = planOf({ ...PAYING, plan: "monthly" });
    const { part, unmount } = await sheetFor(monthly);
    mocks.cancelPlan.mockRejectedValueOnce(refused());
    fireEvent.click(within(part).getByRole("button", { name: "Cancel my plan" }));
    fireEvent.click(within(part).getByRole("button", { name: "Cancel my plan" }));
    expect((await within(part).findByRole("alert")).textContent).toBe(line);
    expect(within(part).getByText(/^Cancel your plan\?/)).toBeTruthy();
    unmount();
    mocks.keepPlan.mockRejectedValueOnce(refused());
    const { part: cancelled } = await sheetFor(planOf({ ...PAYING, plan: "monthly", cancel_scheduled: true }));
    fireEvent.click(within(cancelled).getByRole("button", { name: "Keep my plan" }));
    expect((await within(cancelled).findByRole("alert")).textContent).toBe(line);
    expect(within(cancelled).getByText(`Your plan ends on ${day}.`)).toBeTruthy();
  });

  // Catches: the portal link kept, opened in this tab, blocked by the browser, or opened with
  // a way back into Mothli's page.
  test("payment details open Paddle's portal in a new tab, and the link is not kept", async () => {
    const { part } = await sheetFor(planOf({ ...PAYING, plan: "monthly" }));
    fireEvent.click(within(part).getByRole("button", { name: "Update payment details" }));
    expect(window.open).toHaveBeenCalledWith("", "_blank");
    await waitFor(() => expect(tab.location.href).toBe("https://customer-portal.paddle.com/standin"));
    expect(tab.opener).toBeNull();
    expect(part.innerHTML).not.toContain("customer-portal");
    // A browser that would not open the tab: the link, once, to follow.
    window.open = vi.fn(() => null);
    fireEvent.click(within(part).getByRole("button", { name: "Update payment details" }));
    const link = await within(part).findByRole("link", { name: "Open your payment details" });
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toBe("noopener noreferrer");
    fireEvent.click(link);
    expect(within(part).queryByRole("link")).toBeNull();
    // A failure closes the empty tab and says so.
    window.open = vi.fn(() => tab as unknown as Window);
    mocks.planPortal.mockRejectedValueOnce(new ApiError("server words", 503, "paddle_unavailable"));
    fireEvent.click(within(part).getByRole("button", { name: "Update payment details" }));
    expect((await within(part).findByRole("alert")).textContent).toBe(
      "I can’t reach our payment service just now, so nothing has changed. Please try again in a minute.");
    expect(tab.close).toHaveBeenCalled();
  });

  // Catches: Paddle's script fetched with the account sheet (or any screen but the plan sheet),
  // the reader's email not passed on, or focus lost on the way back.
  test("See the plan opens the plan sheet, which alone fetches Paddle, and comes back", async () => {
    const { part } = await sheetFor(planOf({ state: "free", next_date: DATE }));
    expect(paddleScripts()).toEqual([]);
    fireEvent.click(within(part).getByRole("button", { name: "See the plan" }));
    const plan = await screen.findByRole("dialog", { name: "Keep talking with Mothli" });
    expect(screen.queryByRole("dialog", { name: "Your account" })).toBeNull();
    expect(mocks.plan).toHaveBeenCalledTimes(1);           // the sheet was given the plan
    await waitFor(() => expect(paddleScripts()).toHaveLength(1));
    const paddle = fakePaddle();
    settleScript(paddle);
    mocks.startCheckout.mockResolvedValueOnce({ transaction_id: "txn_standin" });
    fireEvent.click(await within(plan).findByRole("button", { name: "Choose yearly" }));
    await waitFor(() => expect(paddle.opened).toHaveLength(1));
    expect(paddle.opened[0]).toMatchObject({ customer: { email: "wren@example.com" } });
    act(() => paddle.emit("checkout.closed"));
    fireEvent.keyDown(document, { key: "Escape" });
    const back = await screen.findByRole("dialog", { name: "Your account" });
    await waitFor(() => expect(document.activeElement).toBe(within(back).getByRole("button", { name: "See the plan" })));
  });
});

describe("planDay", () => {
  test("a day in the reader's own words, with the year only when it is not this one", () => {
    const now = new Date("2026-11-23T12:00:00Z");
    expect(planDay("2026-12-03T10:00:00+00:00", now, "en-GB", "UTC")).toBe("3 December");
    expect(planDay("2027-12-03T10:00:00+00:00", now, "en-GB", "UTC")).toBe("3 December 2027");
    expect(planDay("2026-12-03T10:00:00+00:00", now, "en-US", "UTC")).toBe("December 3");
    // The reader's own time zone decides the day.
    expect(planDay("2026-12-03T23:30:00+00:00", now, "en-GB", "Asia/Tokyo")).toBe("4 December");
    expect(planDay("not a date", now)).toBe("");
  });
});

// ADR 0045 step 7: deleting the account while paying.
describe("Deleting while paying", () => {
  const steps = () => within(screen.getByRole("dialog", { name: "Delete your account?" }))
    .getAllByRole("listitem").map((item) => item.textContent?.replace(/\s+/g, " "));
  async function confirmFor(plan: Plan) {
    mocks.plan.mockResolvedValue(plan);
    render(<Account session={session} onClose={() => {}} />);
    await waitFor(() => expect(mocks.plan).toHaveBeenCalled());
    await waitFor(() => expect(document.querySelector(".ml-account-plan")?.textContent).not.toMatch(/Looking up/));
    fireEvent.click(screen.getByRole("button", { name: "Delete my account" }));
    return screen.getByRole("dialog", { name: "Delete your account?" });
  }

  // Catches: a paying reader not told their plan stops and they won't be charged again, told a
  // day that isn't true, or told anything with a counter in it.
  test("a paying reader is told, once and plainly, that the plan won't renew", async () => {
    const far = "2099-12-03T10:00:00+00:00";
    const sheet = await confirmFor(planOf({ state: "paying", plan: "yearly", subscribed: true, next_date: far }));
    expect(steps()[1]).toBe(
      "Your plan won’t renew, so you won’t be charged again. Change your mind within the 30 days "
      + `and Mothli is still yours until ${planDay(far)}.`);
    expect(steps()).toHaveLength(4);
    noCounter(sheet);
    expect(await axeAA(sheet)).toHaveNoViolations();
  });

  // Catches: the line shown to a reader with no plan that renews (free weeks, a gift alone).
  test("a reader without a plan that renews hears nothing about one", async () => {
    for (const values of [{ state: "free" as const, subscribed: false },
      { state: "paying" as const, plan: "gift" as const, subscribed: false }]) {
      await confirmFor(planOf({ next_date: null, ...values }));
      expect(steps()).toHaveLength(3);
      expect(steps().join(" ")).not.toMatch(/plan/);
      cleanup();
    }
  });

  test("the day said is whichever comes first: the day paid for, or the account's erasure", () => {
    const now = new Date("2026-11-23T12:00:00Z");
    const soonDate = "2026-12-03T10:00:00+00:00";
    const laterDate = "2027-08-01T10:00:00+00:00";
    const soon = planOf({ state: "paying", plan: "monthly", subscribed: true, next_date: soonDate });
    expect(deletingPlanLine(soon, now)).toBe(
      `Your plan won’t renew, so you won’t be charged again. Change your mind before ${planDay(soonDate, now)} and Mothli is still yours until then.`);
    const later = { ...soon, plan: "yearly" as const, next_date: laterDate };
    expect(deletingPlanLine(later, now)).toBe(
      `Your plan won’t renew, so you won’t be charged again. Change your mind within the 30 days and Mothli is still yours until ${planDay(laterDate, now)}.`);
    // Under a gift the day is the gift's, not the plan's: no day at all.
    expect(deletingPlanLine({ ...soon, plan: "gift" }, now)).toBe(DELETING_PLAN);
    expect(deletingPlanLine({ ...soon, next_date: null }, now)).toBe(DELETING_PLAN);
    for (const line of [deletingPlanLine(soon, now), deletingPlanLine(later, now)])
      expect(line).not.toMatch(/unlimited|questions?|left|hours?|countdown/i);
  });
});
