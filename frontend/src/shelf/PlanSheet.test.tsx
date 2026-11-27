/** The plan sheet, "Keep talking with Mothli": the prices Paddle gives, the checkout the server
 * makes, and the wait after it. Paddle.js is a fake `window.Paddle`; nothing reaches Paddle. */
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { axeAA } from "../test-a11y";

const mocks = vi.hoisted(() => ({ plan: vi.fn(), startCheckout: vi.fn() }));
vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: mocks,
}));
import { ApiError } from "../api";
import { LATE_ASK_EVERY_MS, stopWatchingForLatePlan, watchingForLatePlan } from "../ui/latePlan";
import { forgetPaddle } from "./paddle";
import { fakePaddle, noCounter, paddleScripts, planOf, preview, settleScript } from "./plan-fakes";
import {
  ASK_EVERY_MS,
  CHECKOUT_FAILED,
  CHECKOUT_REFUSED,
  PLAN_LINES,
  PLAN_TITLE_NEW,
  PlanSheet,
  WAIT_AFTER_CLOSING_MS,
  WAIT_AFTER_PAYMENT_MS,
} from "./PlanSheet";

const PAYING = planOf({ state: "paying", plan: "yearly", subscribed: true });
const TRANSACTION = "txn_standin0000000000000000000";

async function openSheet(props: Partial<Parameters<typeof PlanSheet>[0]> = {}, paddle = fakePaddle()) {
  const onClose = props.onClose ?? vi.fn();
  const view = render(<PlanSheet email="wren@example.com" onClose={onClose} {...props} />);
  await waitFor(() => expect(paddleScripts()).toHaveLength(1));
  settleScript(paddle);
  await screen.findByRole("button", { name: PLAN_LINES.chooseYearly });
  return { ...view, paddle, onClose };
}

beforeEach(() => {
  forgetPaddle();
  mocks.plan.mockReset().mockResolvedValue(planOf());
  mocks.startCheckout.mockReset().mockResolvedValue({ transaction_id: TRANSACTION });
});
afterEach(() => {
  vi.useRealTimers();
  forgetPaddle();
  stopWatchingForLatePlan();
});

describe("the plan sheet", () => {
  // Catches: prices written in the app, monthly put first, the yearly month shown without the
  // total it is billed, the saving rounded up or shown when it cannot be computed, the yearly plan
  // chosen for the reader, a line missing, or Paddle's script asked for before the sheet opens.
  test("shows the yearly plan first, as a month with its total, then the monthly, from Paddle", async () => {
    expect(paddleScripts()).toEqual([]);
    render(<PlanSheet email="wren@example.com" onClose={() => {}} />);
    const sheet = screen.getByRole("dialog", { name: "Keep talking with Mothli" });
    expect(within(sheet).getByRole("status").textContent).toBe("Getting the prices for you…");
    expect(document.activeElement).toBe(within(sheet).getByRole("button", { name: "Close" }));
    await waitFor(() => expect(paddleScripts()).toHaveLength(1));
    const paddle = fakePaddle();
    settleScript(paddle);
    await within(sheet).findByRole("button", { name: "Choose yearly" });
    const choices = within(sheet).getAllByRole("listitem");
    expect(choices.map((item) => item.textContent)).toEqual([
      "Yearly$6.67 a month, billed $79.99 a year48% less than paying monthly.Choose yearly",
      "Monthly$12.99 a monthChoose monthly",
    ]);
    expect(within(sheet).getAllByRole("heading").map((heading) => heading.textContent)).toEqual([
      "Keep talking with Mothli", "Yearly", "Monthly"]);
    expect(sheet.textContent).not.toMatch(/best value/i);
    const yearly = within(sheet).getByRole("button", { name: "Choose yearly" });
    expect(document.getElementById(yearly.getAttribute("aria-describedby")!)?.textContent).toBe(
      "$6.67 a month, billed $79.99 a year48% less than paying monthly.");
    // Neither plan is chosen for the reader (the owner's decision, 27 November 2026): both are
    // drawn and offered alike.
    const monthlyButton = within(sheet).getByRole("button", { name: "Choose monthly" });
    expect([yearly.className, choices[0].className]).toEqual([monthlyButton.className, choices[1].className]);
    expect(within(sheet).queryByRole("radio")).toBeNull();
    // The maker's one line; nothing about the technology, and no hurry.
    expect(within(sheet).getByText("Made by one reader, for readers. No ads.")).toBeTruthy();
    expect(sheet.textContent).not.toMatch(/\bAI\b|the chat|model|unlimited|trial|credits|limited time/i);
    // The owner's soft brake (27 November 2026), in Mothli's words, never a number.
    expect(within(sheet).getByText("I keep a gentle daily rhythm, so on a very busy day I might ask to pick up again tomorrow.")).toBeTruthy();
    expect(within(sheet).getByText("Cancel any time from Your account. Your books stay free either way.")).toBeTruthy();
    expect(within(sheet).getByText("Payments are handled by Paddle, who sells Mothli on our behalf.")).toBeTruthy();
    // The public pages (ADR 0045 step 8), opened beside the sheet so it stays as it is.
    expect(["our terms", "how refunds work", "your privacy"].map((name) => {
      const link = within(sheet).getByRole("link", { name });
      return [link.getAttribute("href"), link.getAttribute("target")];
    })).toEqual([["/terms", "_blank"], ["/refunds", "_blank"], ["/privacy", "_blank"]]);
    expect(within(sheet).getByRole("status").textContent).toBe("");
    expect(paddle.calls).toEqual(["environment:sandbox", "initialize:test_clientstandin",
      "preview:pri_monthlystandin,pri_yearlystandin"]);
    noCounter(sheet);
    expect(await axeAA(document.body)).toHaveNoViolations();
  });

  // Catches: "Keep talking" said to a reader who has not talked yet, or anything of the removed
  // free weeks (ADR 0046) said to them.
  test("a reader who has never had Mothli: their own title, and nothing about free weeks", async () => {
    await openSheet({ plan: planOf({ state: "new", why: null, next_date: null }) });
    const sheet = screen.getByRole("dialog", { name: "Talk with Mothli" });
    expect(within(sheet).getAllByRole("heading")[0].textContent).toBe("Talk with Mothli");
    expect(PLAN_TITLE_NEW).toBe("Talk with Mothli");
    expect(sheet.textContent).not.toMatch(/free weeks|two weeks|starts today/);
    expect("instead" in PLAN_LINES || "startsToday" in PLAN_LINES).toBe(false);
    noCounter(sheet);
  });

  // Catches: a line about when the plan starts said to a gifted reader.
  test("a gifted reader is not told their plan starts today", async () => {
    await openSheet({ plan: planOf({ state: "paying", plan: "gift", next_date: null }) });
    expect(screen.queryByText(/starts today/)).toBeNull();
    expect(screen.getByRole("dialog", { name: "Keep talking with Mothli" })).toBeTruthy();
  });

  test("a plan given by the opener is not asked for again", async () => {
    await openSheet({ plan: planOf({ state: "new", next_date: null }) });
    expect(mocks.plan).not.toHaveBeenCalled();
  });

  // Catches: a choice offered with no price Paddle gave for it.
  test("a plan Paddle has no price for is not offered", async () => {
    render(<PlanSheet onClose={() => {}} />);
    await waitFor(() => expect(paddleScripts()).toHaveLength(1));
    settleScript(fakePaddle(preview("USD", [["pri_monthlystandin", "1299", "$12.99"]])));
    // The yearly price is missing: only the monthly choice, and it is the main one.
    const monthly = await screen.findByRole("button", { name: "Choose monthly" });
    expect(monthly.className).toContain("ui-button--primary");
    expect(screen.queryByRole("button", { name: "Choose yearly" })).toBeNull();
  });

  test("Escape closes it, and Tab stays inside", async () => {
    const { onClose } = await openSheet();
    const sheet = screen.getByRole("dialog");
    within(sheet).getByRole("button", { name: "Choose monthly" }).focus();
    fireEvent.keyDown(document, { key: "Tab" });
    expect(sheet.contains(document.activeElement)).toBe(true);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  // Catches: a reader who cannot load Paddle (offline, or blocked) left with an empty sheet or
  // a raw error, and no way to try again.
  test("Paddle's script not loading says so kindly and can be tried again", async () => {
    render(<PlanSheet onClose={() => {}} />);
    await waitFor(() => expect(paddleScripts()).toHaveLength(1));
    settleScript(null);
    const status = await screen.findByText(PLAN_LINES.unreachable);
    expect(status.textContent).toBe(
      "I couldn’t get the checkout ready just now. It might be your connection, or something in your browser blocking it.");
    await waitFor(() => expect(document.activeElement).toBe(status));
    noCounter(document.body);
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(paddleScripts()).toHaveLength(1));
    settleScript(fakePaddle());
    expect(await screen.findByRole("button", { name: "Choose yearly" })).toBeTruthy();
  });

  test("prices Paddle cannot give say the same, kindly", async () => {
    render(<PlanSheet onClose={() => {}} />);
    await waitFor(() => expect(paddleScripts()).toHaveLength(1));
    settleScript(fakePaddle(new Error("offline")));
    expect(await screen.findByText(PLAN_LINES.unreachable)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Choose yearly" })).toBeNull();
  });

  // Catches: Paddle's script fetched, or a checkout offered, where this server cannot make one,
  // or for a reader already on the plan.
  test("where no checkout can open, or the reader is on the plan, Paddle is never asked", async () => {
    mocks.plan.mockResolvedValueOnce(planOf({ checkout: null }));
    const { unmount } = render(<PlanSheet onClose={() => {}} />);
    expect(await screen.findByText(
      "The plan isn’t on offer here just yet. Your books stay free either way.")).toBeTruthy();
    unmount();
    mocks.plan.mockResolvedValueOnce(PAYING);
    render(<PlanSheet onClose={() => {}} />);
    expect(await screen.findByText(
      "Good news: you’re already on the plan! There’s nothing more to do.")).toBeTruthy();
    expect(paddleScripts()).toEqual([]);
    expect(screen.queryByRole("button", { name: /Choose/ })).toBeNull();
  });
});

describe("the checkout", () => {
  // Catches: a checkout opened in the browser for any price (it must be the server's
  // transaction), the reader's email not prefilled, or a checkout opened twice.
  test("opens Paddle's checkout over the page for the server's transaction, email prefilled", async () => {
    const { paddle } = await openSheet();
    fireEvent.click(screen.getByRole("button", { name: "Choose yearly" }));
    expect(screen.getByRole("button", { name: "One moment…" })).toBeTruthy();
    await waitFor(() => expect(paddle.opened).toHaveLength(1));
    expect(mocks.startCheckout).toHaveBeenCalledWith("yearly");
    expect(paddle.opened[0]).toEqual({
      transactionId: TRANSACTION,
      customer: { email: "wren@example.com" },
      settings: { displayMode: "overlay", locale: "en" },
    });
    expect(screen.getByRole("status").textContent).toBe("Your checkout is open. Take your time, I’ll be right here.");
    // Paddle's window has the keyboard: Escape here does not close the sheet behind it.
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.getByRole("dialog")).toBeTruthy();
  });

  test("without an email, none is prefilled", async () => {
    const { paddle } = await openSheet({ email: null });
    fireEvent.click(screen.getByRole("button", { name: "Choose monthly" }));
    await waitFor(() => expect(paddle.opened).toHaveLength(1));
    expect(mocks.startCheckout).toHaveBeenCalledWith("monthly");
    expect(paddle.opened[0]).toEqual({ transactionId: TRANSACTION, settings: { displayMode: "overlay", locale: "en" } });
  });

  // Catches: a refusal shown as a raw error, the same line for every refusal, or the choices
  // taken away so the reader cannot try again.
  test.each([
    ["checkout_busy", 429, "We’ve opened a lot of checkouts today, so let’s take a little break. Please try again later."],
    ["plan_not_sold", 404, "That plan isn’t on offer right now. Sorry about that!"],
    ["paddle_unavailable", 503, "I can’t reach our payment service just now, so nothing has changed. Please try again in a minute."],
    [null, 0, "I couldn’t start the checkout just now. Check your connection and try again."],
  ])("a refusal (%s) has its own kind line", async (code, status, line) => {
    mocks.startCheckout.mockRejectedValueOnce(
      code ? new ApiError("server words", status, code) : new TypeError("offline"));
    const { paddle } = await openSheet();
    fireEvent.click(screen.getByRole("button", { name: "Choose yearly" }));
    expect((await screen.findByRole("alert")).textContent).toBe(line);
    expect(code ? CHECKOUT_REFUSED[code] : CHECKOUT_FAILED).toBe(line);
    expect(paddle.opened).toEqual([]);
    expect((screen.getByRole("button", { name: "Choose yearly" }) as HTMLButtonElement).disabled).toBe(false);
    // Focus does not fall to the page (M5): back on the button pressed, enabled again.
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("button", { name: "Choose yearly" })));
    noCounter(document.body);
  });

  test("a refusal of the monthly choice puts focus back on it", async () => {
    mocks.startCheckout.mockRejectedValueOnce(new ApiError("server words", 429, "checkout_busy"));
    await openSheet();
    fireEvent.click(screen.getByRole("button", { name: "Choose monthly" }));
    await screen.findByRole("alert");
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("button", { name: "Choose monthly" })));
  });

  test("already on the plan: says so, warmly, and offers nothing more", async () => {
    mocks.startCheckout.mockRejectedValueOnce(new ApiError("You’re already on the plan!", 409, "already_subscribed"));
    await openSheet();
    fireEvent.click(screen.getByRole("button", { name: "Choose yearly" }));
    expect(await screen.findByText("Good news: you’re already on the plan! There’s nothing more to do.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Choose/ })).toBeNull();
  });
});

describe("after the checkout", () => {
  async function paying(onJoined = vi.fn()) {
    const opened = await openSheet({ onJoined });
    fireEvent.click(screen.getByRole("button", { name: "Choose yearly" }));
    await waitFor(() => expect(opened.paddle.opened).toHaveLength(1));
    vi.useFakeTimers();
    return { ...opened, onJoined };
  }

  // Catches: the app deciding the reader is on the plan because Paddle's window said so (only
  // the server's word counts), or a wait with no end.
  test("a completed payment: one moment, then on the plan once the server says so", async () => {
    const { paddle, onJoined } = await paying();
    mocks.plan.mockReset().mockResolvedValueOnce(planOf()).mockResolvedValue(PAYING);
    act(() => paddle.emit("checkout.completed"));
    const status = screen.getByRole("status");
    expect(status.textContent).toBe("One moment, I’m getting ready…");
    expect(document.activeElement).toBe(status);
    expect(onJoined).not.toHaveBeenCalled();
    await act(() => vi.advanceTimersByTimeAsync(ASK_EVERY_MS));
    expect(status.textContent).toBe("One moment, I’m getting ready…");
    await act(() => vi.advanceTimersByTimeAsync(ASK_EVERY_MS));
    expect(status.textContent).toBe("You’re on the plan! I’m so glad we get to keep talking.");
    expect(onJoined).toHaveBeenCalledWith(PAYING);
    expect(mocks.plan).toHaveBeenCalledTimes(2);
    // Closing Paddle's window afterwards changes nothing.
    act(() => paddle.emit("checkout.closed"));
    expect(status.textContent).toBe("You’re on the plan! I’m so glad we get to keep talking.");
    expect(screen.getByRole("button", { name: "Done" })).toBeTruthy();
    noCounter(document.body);
  });

  // Catches: a slow message from Paddle shown as an error, or as if the money were lost, or the
  // reader sent away to look for it themselves.
  test("a payment the server has not heard of in time: it may be on its way, and I’ll open up here", async () => {
    const { paddle, onClose } = await paying();
    mocks.plan.mockReset().mockResolvedValue(planOf());
    act(() => paddle.emit("checkout.completed"));
    await act(() => vi.advanceTimersByTimeAsync(WAIT_AFTER_PAYMENT_MS - ASK_EVERY_MS));
    expect(screen.getByRole("status").textContent).toBe("One moment, I’m getting ready…");
    await act(() => vi.advanceTimersByTimeAsync(ASK_EVERY_MS * 2));
    const status = screen.getByRole("status");
    expect(status.textContent).toBe(
      "Your payment may still be on its way to me. That can take a few minutes. There’s nothing you need to do: as soon as it arrives, I’ll open up right here by myself.");
    expect(PLAN_LINES.slow).toBe(status.textContent);
    expect(status.textContent).not.toMatch(/Your account|reopen|reload/i);
    expect(screen.queryByRole("alert")).toBeNull();
    // Paid: no way back to the plans, which could pay twice.
    expect(screen.queryByRole("button", { name: /Choose/ })).toBeNull();
    // The sheet's own "Close" button, after the corner one.
    fireEvent.click(screen.getAllByRole("button", { name: "Close" })[1]);
    expect(onClose).toHaveBeenCalledTimes(1);
    noCounter(document.body);
  });

  // Catches: a late payment that never opens the sheet up (H2), or a watch with no end.
  test("a payment that arrives after the wait: the open sheet says so, without a reload", async () => {
    const { paddle, onJoined } = await paying();
    mocks.plan.mockReset().mockResolvedValue(planOf());
    act(() => paddle.emit("checkout.completed"));
    await act(() => vi.advanceTimersByTimeAsync(WAIT_AFTER_PAYMENT_MS + ASK_EVERY_MS));
    expect(screen.getByRole("status").textContent).toBe(PLAN_LINES.slow);
    expect(watchingForLatePlan()).toBe(true);
    // The quiet watch asks every so often; the payment arrives a few minutes later.
    await act(() => vi.advanceTimersByTimeAsync(LATE_ASK_EVERY_MS * 3));
    expect(onJoined).not.toHaveBeenCalled();
    mocks.plan.mockResolvedValue(PAYING);
    await act(() => vi.advanceTimersByTimeAsync(LATE_ASK_EVERY_MS));
    expect(screen.getByRole("status").textContent).toBe("You’re on the plan! I’m so glad we get to keep talking.");
    expect(onJoined).toHaveBeenCalledWith(PAYING);
    expect(watchingForLatePlan()).toBe(false);
    expect(screen.getByRole("button", { name: "Done" })).toBeTruthy();
  });

  // Catches: a checkout reported completed then told "nothing has changed" because the closed
  // event was read against a phase not yet rendered (H3), in either order, in the same tick.
  test.each([
    ["completed, then closed", ["checkout.completed", "checkout.closed"]],
    ["closed, then completed", ["checkout.closed", "checkout.completed"]],
  ])("both events in the same tick (%s): the wait for a payment, never nothing changed", async (_order, events) => {
    const { paddle } = await paying();
    mocks.plan.mockReset().mockResolvedValue(planOf());
    act(() => {
      for (const name of events) paddle.emit(name);
    });
    expect(screen.getByRole("status").textContent).toBe("One moment, I’m getting ready…");
    await act(() => vi.advanceTimersByTimeAsync(WAIT_AFTER_CLOSING_MS + ASK_EVERY_MS));
    expect(screen.getByRole("status").textContent).toBe("One moment, I’m getting ready…");
    expect(document.body.textContent).not.toMatch(/nothing has changed/);
    expect(screen.queryByRole("button", { name: /Choose/ })).toBeNull();
    await act(() => vi.advanceTimersByTimeAsync(WAIT_AFTER_PAYMENT_MS));
    expect(screen.getByRole("status").textContent).toBe(PLAN_LINES.slow);
  });

  test("a completed event after the calm line still waits for the payment", async () => {
    const { paddle } = await paying();
    mocks.plan.mockReset().mockResolvedValue(planOf());
    act(() => paddle.emit("checkout.closed"));
    await act(() => vi.advanceTimersByTimeAsync(WAIT_AFTER_CLOSING_MS + ASK_EVERY_MS));
    expect(screen.getByRole("status").textContent).toBe(PLAN_LINES.nothingChanged);
    act(() => paddle.emit("checkout.completed"));
    expect(screen.getByRole("status").textContent).toBe("One moment, I’m getting ready…");
    expect(screen.queryByRole("button", { name: /Choose/ })).toBeNull();
  });

  // Catches: a reader who closed the checkout without paying told a payment may be on its way
  // (untrue and worrying), or left without the plans.
  test("a checkout closed without paying: a short neutral check, then the plans again, calmly", async () => {
    const { paddle } = await paying();
    mocks.plan.mockReset().mockResolvedValue(planOf());
    act(() => paddle.emit("checkout.closed"));
    const status = screen.getByRole("status");
    expect(status.textContent).toBe("One moment…");
    expect(document.activeElement).toBe(status);
    expect(screen.queryByRole("button", { name: /Choose/ })).toBeNull();
    await act(() => vi.advanceTimersByTimeAsync(WAIT_AFTER_CLOSING_MS + ASK_EVERY_MS));
    expect(status.textContent).toBe("No worries, nothing has changed. Pick a plan whenever you like.");
    expect(document.activeElement).toBe(status);
    expect(document.body.textContent).not.toMatch(/payment may still be on its way/);
    expect(screen.queryAllByRole("button", { name: "Close" })).toHaveLength(1);   // the corner one only
    expect(mocks.plan.mock.calls.length).toBeLessThanOrEqual(WAIT_AFTER_CLOSING_MS / ASK_EVERY_MS);
    noCounter(document.body);
    // The choices are simply there again; choosing clears the line.
    vi.useRealTimers();
    fireEvent.click(screen.getByRole("button", { name: "Choose monthly" }));
    await waitFor(() => expect(paddle.opened).toHaveLength(2));
    expect(status.textContent).toBe("Your checkout is open. Take your time, I’ll be right here.");
  });

  test("a closed checkout the server finds paid after all: on the plan", async () => {
    const { paddle, onJoined } = await paying();
    mocks.plan.mockReset().mockResolvedValue(PAYING);
    act(() => paddle.emit("checkout.closed"));
    await act(() => vi.advanceTimersByTimeAsync(ASK_EVERY_MS));
    expect(screen.getByRole("status").textContent).toBe("You’re on the plan! I’m so glad we get to keep talking.");
    expect(onJoined).toHaveBeenCalledWith(PAYING);
  });

  test("a completed event during the short check starts the full wait", async () => {
    const { paddle } = await paying();
    mocks.plan.mockReset().mockResolvedValue(planOf());
    act(() => paddle.emit("checkout.closed"));
    act(() => paddle.emit("checkout.completed"));
    expect(screen.getByRole("status").textContent).toBe("One moment, I’m getting ready…");
    await act(() => vi.advanceTimersByTimeAsync(WAIT_AFTER_CLOSING_MS + ASK_EVERY_MS));
    expect(screen.getByRole("status").textContent).toBe("One moment, I’m getting ready…");
    await act(() => vi.advanceTimersByTimeAsync(WAIT_AFTER_PAYMENT_MS));
    expect(screen.getByRole("status").textContent).toMatch(/^Your payment may still be on its way to me\./);
  });

  test("a passing failure while waiting is asked again", async () => {
    const { paddle } = await paying();
    mocks.plan.mockReset().mockRejectedValueOnce(new TypeError("offline")).mockResolvedValue(PAYING);
    act(() => paddle.emit("checkout.completed"));
    await act(() => vi.advanceTimersByTimeAsync(ASK_EVERY_MS * 2));
    expect(screen.getByRole("status").textContent).toBe("You’re on the plan! I’m so glad we get to keep talking.");
  });
});
