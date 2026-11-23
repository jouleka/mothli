/** Mothli's payment-link page (`/?_ptxn=txn_…`): where Paddle's emails about a checkout bring a
 * reader, now that every checkout carries Mothli's own address instead of the shared Paddle
 * account's default page. Signed in: the plan sheet opens that checkout, then the same wait.
 * Signed out: the checkout opens anyway, and they are told to sign in to see their plan. Paddle.js
 * is a fake `window.Paddle`; nothing reaches Paddle. */
import { act, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import type { OfflineSession } from "../pwa/offline";
import { axeAA } from "../test-a11y";

const mocks = vi.hoisted(() => ({ plan: vi.fn(), paymentLink: vi.fn(), startCheckout: vi.fn() }));
vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: mocks,
}));
import { forgetPaddle, forgetPaymentLink, paymentLinkTransaction } from "./paddle";
import { PAYMENT_LINK_LINES } from "./PaymentLink";
import { fakePaddle, noCounter, paddleScripts, planOf, settleScript } from "./plan-fakes";
import { ASK_EVERY_MS, PLAN_LINES } from "./PlanSheet";
import { SignInGate } from "./SignIn";

const TRANSACTION = "txn_standin0000000000000000000";
const signedIn: OfflineSession = {
  user: { id: "05dbebd1-e9e8-4be8-8172-eb65ca8d2a67", display_name: "Wren Example", email: "wren@example.com" },
  expires_at: "2099-01-01T00:00:00+00:00",
};
const theApp = () => <p>The shelf</p>;

beforeEach(() => {
  forgetPaddle();
  history.replaceState(null, "", "/");
  sessionStorage.clear();
  mocks.plan.mockReset().mockResolvedValue(planOf());
  mocks.paymentLink.mockReset().mockResolvedValue({ environment: "sandbox", client_token: "test_clientstandin" });
  mocks.startCheckout.mockReset();
});
afterEach(() => {
  vi.useRealTimers();
  forgetPaddle();
  history.replaceState(null, "", "/");
});

describe("reading the payment link", () => {
  // Catches: Paddle.js loaded for anything that merely looks like a link (two ids, another kind
  // of id, the wrong length or case, extra characters), or `_ptxn` left in the address for
  // Paddle.js to open a second checkout from.
  test("only one well-formed transaction id is a payment link", () => {
    expect(paymentLinkTransaction(`?_ptxn=${TRANSACTION}`)).toBe(TRANSACTION);
    expect(paymentLinkTransaction(`?library=1&_ptxn=${TRANSACTION}`)).toBe(TRANSACTION);
    for (const search of ["", "?_ptxn=", `?_ptxn=${TRANSACTION}&_ptxn=${TRANSACTION}`,
      `?_ptxn=${TRANSACTION.toUpperCase()}`, `?_ptxn=${TRANSACTION}0`, `?_ptxn=${TRANSACTION.slice(0, -1)}`,
      `?_ptxn=${TRANSACTION}%3Cscript%3E`, "?_ptxn=sub_standin0000000000000000000",
      `?ptxn=${TRANSACTION}`, `?_ptxn= ${TRANSACTION}`])
      expect(paymentLinkTransaction(search)).toBeNull();
  });

  test("forgetting it keeps the rest of the address", () => {
    history.replaceState(null, "", `/?library=1&_ptxn=${TRANSACTION}`);
    forgetPaymentLink();
    expect(`${location.pathname}${location.search}`).toBe("/?library=1");
    history.replaceState(null, "", `/?_ptxn=${TRANSACTION}`);
    forgetPaymentLink();
    expect(`${location.pathname}${location.search}`).toBe("/");
  });
});

describe("signed in", () => {
  // Catches: the link ignored (the reader lands on the shelf with no checkout), Paddle.js left to
  // open its own second checkout from the address, the email not prefilled, or no wait after.
  test("the plan sheet opens that checkout, then waits for the plan as it always does", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    history.replaceState(null, "", `/?_ptxn=${TRANSACTION}`);
    render(<SignInGate session={signedIn}>{theApp()}</SignInGate>);
    expect(screen.getByText("The shelf")).toBeTruthy();
    const sheet = screen.getByRole("dialog", { name: "Keep talking with Mothli" });
    expect(within(sheet).getByRole("status").textContent).toBe("One moment, I’m opening your checkout…");
    expect(location.search).toBe("");
    await waitFor(() => expect(paddleScripts()).toHaveLength(1));
    const paddle = fakePaddle();
    settleScript(paddle);
    await waitFor(() => expect(paddle.opened).toHaveLength(1));
    expect(paddle.opened[0]).toEqual({
      transactionId: TRANSACTION, customer: { email: "wren@example.com" },
      settings: { displayMode: "overlay", locale: "en" },
    });
    expect(within(sheet).getByRole("status").textContent).toBe(PLAN_LINES.paying);
    expect(mocks.startCheckout).not.toHaveBeenCalled();
    expect(mocks.paymentLink).not.toHaveBeenCalled();

    mocks.plan.mockResolvedValue(planOf({ state: "paying", plan: "yearly", subscribed: true }));
    act(() => paddle.emit("checkout.completed"));
    expect(within(sheet).getByRole("status").textContent).toBe(PLAN_LINES.waiting);
    await act(async () => { await vi.advanceTimersByTimeAsync(ASK_EVERY_MS); });
    expect(within(sheet).getByRole("status").textContent).toBe(PLAN_LINES.joined);
    noCounter(sheet);
  });

  // Catches: a reader already on the plan charged again from an old email's link.
  test("already on the plan: says so, and opens no checkout", async () => {
    mocks.plan.mockResolvedValue(planOf({ state: "paying", plan: "monthly", subscribed: true }));
    history.replaceState(null, "", `/?_ptxn=${TRANSACTION}`);
    render(<SignInGate session={signedIn}>{theApp()}</SignInGate>);
    const sheet = screen.getByRole("dialog", { name: "Keep talking with Mothli" });
    await waitFor(() => expect(within(sheet).getByRole("status").textContent).toBe(PLAN_LINES.already));
    expect(paddleScripts()).toEqual([]);
  });

  test("closing the sheet leaves the shelf, with no link left behind", async () => {
    history.replaceState(null, "", `/?_ptxn=${TRANSACTION}`);
    render(<SignInGate session={signedIn}>{theApp()}</SignInGate>);
    act(() => screen.getByRole("button", { name: "Close" }).click());
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByText("The shelf")).toBeTruthy();
    expect(location.search).toBe("");
  });
});

describe("signed out", () => {
  // Catches: a visitor from Paddle's email on another device made to sign in before they can
  // pay; the checkout opened with another reader's email; Paddle.js loaded with anything but the
  // public read; no word on where their plan will be.
  test("the checkout opens anyway, and they are told to sign in to see their plan", async () => {
    history.replaceState(null, "", `/?_ptxn=${TRANSACTION}`);
    const { container } = render(<SignInGate session={null}>{theApp()}</SignInGate>);
    expect(screen.queryByText("The shelf")).toBeNull();
    expect(screen.getByRole("status").textContent).toBe(PAYMENT_LINK_LINES.opening);
    expect(location.search).toBe("");
    const door = screen.getByRole("link", { name: "Sign in to start reading" });
    expect(door.getAttribute("href")).toBe(`/api/auth/login?return_to=%2F%3F_ptxn%3D${TRANSACTION}`);
    await waitFor(() => expect(paddleScripts()).toHaveLength(1));
    const paddle = fakePaddle();
    settleScript(paddle);
    await waitFor(() => expect(paddle.opened).toHaveLength(1));
    expect(paddle.calls).toEqual(["environment:sandbox", "initialize:test_clientstandin"]);
    expect(paddle.opened[0]).toEqual({ transactionId: TRANSACTION, settings: { displayMode: "overlay", locale: "en" } });
    expect(mocks.paymentLink).toHaveBeenCalledTimes(1);
    expect(mocks.plan).not.toHaveBeenCalled();
    expect(screen.getByRole("status").textContent).toBe(PAYMENT_LINK_LINES.paying);

    act(() => paddle.emit("checkout.completed"));
    const said = screen.getByRole("status");
    expect(said.textContent).toBe("Thank you! Sign in and you’ll find your plan waiting in Your account.");
    expect(document.activeElement).toBe(said);
    // Paid: signing in goes to the shelf, not back to a checkout already paid.
    expect(door.getAttribute("href")).toBe("/api/auth/login?return_to=%2F");
    act(() => paddle.emit("checkout.closed"));
    expect(screen.getByRole("status").textContent).toBe(PAYMENT_LINK_LINES.paid);
    noCounter(container);
    expect(await axeAA(container)).toHaveNoViolations();
  });

  test("a checkout closed without paying: calmly, and signing in opens it again", async () => {
    history.replaceState(null, "", `/?_ptxn=${TRANSACTION}`);
    render(<SignInGate session={null}>{theApp()}</SignInGate>);
    await waitFor(() => expect(paddleScripts()).toHaveLength(1));
    const paddle = fakePaddle();
    settleScript(paddle);
    await waitFor(() => expect(paddle.opened).toHaveLength(1));
    act(() => paddle.emit("checkout.closed"));
    expect(screen.getByRole("status").textContent).toBe("No worries, nothing has changed. Sign in and I’ll open your checkout again.");
    expect(screen.getByRole("link", { name: "Sign in to start reading" }).getAttribute("href"))
      .toBe(`/api/auth/login?return_to=%2F%3F_ptxn%3D${TRANSACTION}`);
  });

  test("a checkout that cannot open says so kindly and can be tried again", async () => {
    mocks.paymentLink.mockRejectedValueOnce(new Error("404"));
    history.replaceState(null, "", `/?_ptxn=${TRANSACTION}`);
    render(<SignInGate session={null}>{theApp()}</SignInGate>);
    await waitFor(() => expect(screen.getByRole("status").textContent).toBe(PLAN_LINES.unreachable));
    expect(paddleScripts()).toEqual([]);
    act(() => screen.getByRole("button", { name: "Try again" }).click());
    await waitFor(() => expect(paddleScripts()).toHaveLength(1));
    const paddle = fakePaddle();
    settleScript(paddle);
    await waitFor(() => expect(paddle.opened).toHaveLength(1));
  });
});

describe("Paddle.js on no other screen", () => {
  // Catches: Paddle's script fetched on the welcome or the shelf, in local mode, or for a link
  // that is not well formed (which is ignored: nothing read, nothing loaded, the address as it
  // was).
  test.each([
    ["the welcome", null, "/"],
    ["the shelf", signedIn, "/"],
    ["the welcome, a malformed link", null, "/?_ptxn=txn_nottherightshape"],
    ["the shelf, a malformed link", signedIn, `/?_ptxn=${TRANSACTION}&_ptxn=${TRANSACTION}`],
    ["local mode, a well-formed link", undefined, `/?_ptxn=${TRANSACTION}`],
  ])("%s", async (_name, session, address) => {
    history.replaceState(null, "", address);
    render(<SignInGate session={session as OfflineSession | null | undefined}>{theApp()}</SignInGate>);
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)); });
    expect(paddleScripts()).toEqual([]);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(mocks.paymentLink).not.toHaveBeenCalled();
    expect(mocks.plan).not.toHaveBeenCalled();
    expect(`${location.pathname}${location.search}`).toBe(address);
  });
});
