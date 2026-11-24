/** The hosted welcome and the gate in front of the app: local mode is untouched, a signed-out
 * reader gets one way in, and a sign-in that ends mid-read brings them back to the same page. */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { announcePendingDeletion, announceSignedOut, type OfflineSession } from "../pwa/offline";
import { axeAA } from "../test-a11y";

const mocks = vi.hoisted(() => ({
  keepAccount: vi.fn(), logout: vi.fn(), exportAccount: vi.fn(), planEnding: vi.fn(), keepPlan: vi.fn(),
}));
vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: mocks,
}));
import { ApiError } from "../api";
import { noCounter } from "./plan-fakes";
import {
  SIGN_IN_AGAIN_LINE, SignIn, SignInGate, erasureDay, keptWithoutPlan, noteAccountDeleted, signInHref, takeKeptWithoutPlan,
} from "./SignIn";
import { planDay } from "./YourPlan";

const signedIn: OfflineSession = {
  user: { id: "05dbebd1-e9e8-4be8-8172-eb65ca8d2a67", display_name: "Wren Example" },
  expires_at: "2099-01-01T00:00:00+00:00",
};

const theApp = () => <p>The shelf</p>;

beforeEach(() => {
  history.replaceState(null, "", "/");
  sessionStorage.clear();
});

afterEach(() => {
  history.replaceState(null, "", "/");
  sessionStorage.clear();
});

describe("SignInGate", () => {
  // Catches: any sign-in screen, link or listener leaking into local mode.
  test("local mode shows the app and never the welcome, even if a request says 401", () => {
    render(<SignInGate session={undefined}>{theApp()}</SignInGate>);
    expect(screen.getByText("The shelf")).toBeTruthy();
    act(() => announceSignedOut());
    expect(screen.getByText("The shelf")).toBeTruthy();
    expect(screen.queryByRole("link", { name: /sign in/i })).toBeNull();
  });

  test("hosted and signed out: the welcome, with one way in and no password field", async () => {
    history.replaceState(null, "", "/?book=b1");
    const { container } = render(<SignInGate session={null}>{theApp()}</SignInGate>);
    expect(screen.queryByText("The shelf")).toBeNull();
    expect(screen.getByRole("heading", { level: 1, name: "Read with a friend in the margin." })).toBeTruthy();
    // One way in, offered on the welcome and again beside the prices (L9 of the plan walk):
    // the same door; every other link is the footer's (the public pages, ADR 0045 step 8).
    const doors = screen.getAllByRole("link", { name: "Sign in to start reading" });
    expect(doors).toHaveLength(2);
    const [door, again] = doors;
    expect(door.getAttribute("href")).toBe("/api/auth/login?return_to=%2F%3Fbook%3Db1");
    expect(again.getAttribute("href")).toBe(door.getAttribute("href"));
    expect(again.closest(".pp-about")).toBeTruthy();
    const footer = screen.getByRole("navigation", { name: "About Mothli" });
    expect(screen.getAllByRole("link").filter((link) => !footer.contains(link))).toEqual(doors);
    expect(container.querySelector("form, input")).toBeNull();
    expect(await axeAA(container)).toHaveNoViolations();
  });

  // Catches (ADR 0045 section 6, "At sign-up"): the welcome not saying reading is free and Mothli
  // free for the first two weeks, or saying it with a number or a countdown; or the line on the
  // welcome-back of a reader signed out mid-read, who already knows.
  test("the welcome says, once, that reading is free and Mothli free for the first two weeks", () => {
    const line = "Reading here is free, always. Talking with me is free for your first two weeks.";
    const first = render(<SignInGate session={null}>{theApp()}</SignInGate>);
    expect(screen.getAllByText(line)).toHaveLength(1);
    expect(first.container.textContent).not.toMatch(/\d|unlimited|questions? left/i);
    first.unmount();
    render(<SignInGate session={signedIn}>{theApp()}</SignInGate>);
    act(() => announceSignedOut());
    expect(screen.queryByText(line)).toBeNull();
  });

  // Catches: a reader signed out mid-read left on failing screens, or sent back to the shelf
  // instead of the book they were in.
  test("hosted and signed in: a 401 brings the welcome back, pointing at the same page", () => {
    history.replaceState(null, "", "/?book=b1");
    render(<SignInGate session={signedIn}>{theApp()}</SignInGate>);
    expect(screen.getByText("The shelf")).toBeTruthy();
    act(() => announceSignedOut());
    expect(screen.queryByText("The shelf")).toBeNull();
    expect(screen.getByRole("heading", { level: 1, name: "Let’s get you back to your page." })).toBeTruthy();
    expect(screen.getByRole("link", { name: "Sign in again" }).getAttribute("href"))
      .toBe("/api/auth/login?return_to=%2F%3Fbook%3Db1");
  });

  // Catches: a sign-in that keeps failing looking as if nothing happened. The reader is never
  // sent off again by itself: the welcome only says so and waits for the button.
  test("coming back still signed out says it didn't work, once, and does not leave by itself", () => {
    const first = render(<SignInGate session={null}>{theApp()}</SignInGate>);
    expect(screen.queryByText(/didn’t quite work/)).toBeNull();
    // jsdom cannot follow the link; only the click itself matters here.
    const stay = (event: Event) => event.preventDefault();
    window.addEventListener("click", stay);
    // The door beside the prices does the same as the welcome's.
    fireEvent.click(screen.getAllByRole("link", { name: "Sign in to start reading" })[1]);
    window.removeEventListener("click", stay);
    first.unmount();
    render(<SignInGate session={null}>{theApp()}</SignInGate>);
    expect(screen.getByRole("status").textContent).toBe("Hmm, that didn’t quite work. Let’s try once more.");
  });

  // Catches (2026-11-24): a reader back from the sign-in service too late seeing a page of code,
  // or a cold line, or the line again on every reload.
  test("back from signing in without a session: one kind line, the button, and a clean address", async () => {
    history.replaceState(null, "", "/?signin=again");
    // An attempt noted too: only the one line is said.
    sessionStorage.setItem("mothli.sign-in-attempt", String(Date.now()));
    const { container } = render(<SignInGate session={null}>{theApp()}</SignInGate>);
    const status = screen.getByRole("status");
    expect(status.textContent).toBe(SIGN_IN_AGAIN_LINE);
    expect(SIGN_IN_AGAIN_LINE).not.toMatch(/error|session|expired|token/i);
    expect(screen.queryByText(/didn’t quite work/)).toBeNull();
    const [door] = screen.getAllByRole("link", { name: "Sign in to start reading" });
    expect(door.getAttribute("href")).toBe("/api/auth/login?return_to=%2F");
    expect(window.location.search).toBe("");
    expect(await axeAA(container)).toHaveNoViolations();
  });

  test("the marker leaves the rest of the address alone, and a reload says nothing", () => {
    history.replaceState(null, "", "/?book=b1&signin=again");
    const first = render(<SignInGate session={null}>{theApp()}</SignInGate>);
    expect(screen.getByText(SIGN_IN_AGAIN_LINE)).toBeTruthy();
    expect(`${window.location.pathname}${window.location.search}`).toBe("/?book=b1");
    first.unmount();
    render(<SignInGate session={null}>{theApp()}</SignInGate>);
    expect(screen.queryByText(SIGN_IN_AGAIN_LINE)).toBeNull();
  });

  test("already signed in: the marker says nothing and is taken out of the address", () => {
    history.replaceState(null, "", "/?signin=again");
    render(<SignInGate session={signedIn}>{theApp()}</SignInGate>);
    expect(screen.getByText("The shelf")).toBeTruthy();
    expect(screen.queryByText(SIGN_IN_AGAIN_LINE)).toBeNull();
    expect(window.location.search).toBe("");
  });

  test("a successful sign-in forgets the earlier attempt", () => {
    sessionStorage.setItem("mothli.sign-in-attempt", String(Date.now()));
    render(<SignInGate session={signedIn}>{theApp()}</SignInGate>);
    expect(sessionStorage.getItem("mothli.sign-in-attempt")).toBeNull();
  });
});

describe("waiting to be erased", () => {
  const pending: OfflineSession = { ...signedIn, erase_after: "2099-12-18T00:00:00+00:00" };
  const assign = vi.fn();
  const realLocation = window.location;
  beforeEach(() => {
    mocks.keepAccount.mockReset().mockResolvedValue(undefined);
    mocks.logout.mockReset().mockResolvedValue(undefined);
    // No plan that won't renew, unless a test says so.
    mocks.planEnding.mockReset().mockResolvedValue({ wont_renew: false, ends: null });
    mocks.keepPlan.mockReset().mockResolvedValue({});
    assign.mockReset();
    Object.defineProperty(window, "location", {
      configurable: true, value: { ...realLocation, assign, pathname: "/", search: "?book=b1" },
    });
  });
  afterEach(() => {
    Object.defineProperty(window, "location", { configurable: true, value: realLocation });
  });

  // Catches: the app given back to a reader who asked to delete their account, or the date
  // written as a machine would write it.
  test("signing in again shows only the choice, with the day said as a person would", async () => {
    const { container } = render(<SignInGate session={pending}>{theApp()}</SignInGate>);
    expect(screen.queryByText("The shelf")).toBeNull();
    const heading = screen.getByRole("heading", { level: 1 });
    // The day where the reader is: the last moment before the erasure, in their own zone.
    expect(heading.textContent).toBe(`Your account is set to be erased ${erasureDay(pending.erase_after!)}.`);
    expect(heading.textContent).toMatch(/^Your account is set to be erased on (Thursday 17|Friday 18) December 2099\.$/);
    expect(document.activeElement).toBe(heading);
    expect(screen.getByRole("button", { name: "Keep my account" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Sign out" })).toBeTruthy();
    expect(container.textContent).not.toMatch(/pending|revoked|purge|deletion/i);
    expect(await axeAA(container)).toHaveNoViolations();
  });

  test("keeping the account opens the app again where the reader was", async () => {
    render(<SignInGate session={pending}>{theApp()}</SignInGate>);
    fireEvent.click(screen.getByRole("button", { name: "Keep my account" }));
    await waitFor(() => expect(assign).toHaveBeenCalledWith("/?book=b1"));
    expect(mocks.keepAccount).toHaveBeenCalledTimes(1);
    expect(mocks.logout).not.toHaveBeenCalled();
  });

  test("a keep that fails says so kindly and can be tried again", async () => {
    mocks.keepAccount.mockRejectedValueOnce(new TypeError("offline"));
    render(<SignInGate session={pending}>{theApp()}</SignInGate>);
    fireEvent.click(screen.getByRole("button", { name: "Keep my account" }));
    expect((await screen.findByRole("alert")).textContent).toBe(
      "That didn’t work just now. Check your connection and try again.");
    expect(assign).not.toHaveBeenCalled();
    expect((screen.getByRole("button", { name: "Keep my account" }) as HTMLButtonElement).disabled).toBe(false);
  });

  // Catches: a reader on their way out with no way to take what is theirs; the copy offered
  // to a reader who is simply signed out.
  test("the one more thing they may do is take a copy of their data", async () => {
    mocks.exportAccount.mockReset().mockResolvedValue({
      blob: new Blob(["zip"]), name: "mothli-my-data-2026-11-17.zip" });
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    const realCreate = URL.createObjectURL;
    URL.createObjectURL = vi.fn(() => "blob:made-up");
    try {
      const { container } = render(<SignInGate session={pending}>{theApp()}</SignInGate>);
      const door = screen.getByRole("button", { name: "Take a copy of my data" });
      expect(document.getElementById(door.getAttribute("aria-describedby")!)?.textContent).toBe(
        "Leaving anyway? Take everything that’s yours with you first, in one file. The books themselves aren’t in it.");
      fireEvent.click(door);
      await waitFor(() => expect(screen.getByRole("status").textContent).toBe(
        "Done. Your copy is in your downloads."));
      expect(click).toHaveBeenCalledTimes(1);
      expect(mocks.keepAccount).not.toHaveBeenCalled();
      expect(mocks.logout).not.toHaveBeenCalled();
      expect(await axeAA(container)).toHaveNoViolations();
    } finally {
      URL.createObjectURL = realCreate;
      click.mockRestore();
    }
    cleanup();
    render(<SignInGate session={null}>{theApp()}</SignInGate>);
    expect(screen.queryByRole("button", { name: "Take a copy of my data" })).toBeNull();
  });

  test("signing out goes back to the welcome", async () => {
    render(<SignInGate session={pending}>{theApp()}</SignInGate>);
    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
    await waitFor(() => expect(assign).toHaveBeenCalledWith("/"));
    expect(mocks.keepAccount).not.toHaveBeenCalled();
  });

  // Catches: an open app that keeps showing failing screens once the server says the account
  // waits to be erased (asked for on another device).
  test("a request answered with the waiting refusal brings the choice up", () => {
    render(<SignInGate session={signedIn}>{theApp()}</SignInGate>);
    act(() => announcePendingDeletion("2099-12-18T00:00:00+00:00"));
    expect(screen.queryByText("The shelf")).toBeNull();
    expect(screen.getByRole("button", { name: "Keep my account" })).toBeTruthy();
  });

  // Catches: anything of this leaking into local mode.
  test("local mode ignores it", () => {
    render(<SignInGate session={undefined}>{theApp()}</SignInGate>);
    act(() => announcePendingDeletion("2099-12-18T00:00:00+00:00"));
    expect(screen.getByText("The shelf")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Keep my account" })).toBeNull();
  });

  test("after asking, the welcome says it is done and names the day, until signing in", () => {
    noteAccountDeleted("2099-12-18T00:00:00+00:00");
    render(<SignInGate session={null}>{theApp()}</SignInGate>);
    expect(screen.getByRole("status").textContent).toBe(
      `Done. You’re signed out everywhere, and your account will be erased ${erasureDay("2099-12-18T00:00:00+00:00")} `
      + "unless you sign in again before then.");
    // The goodbye promises no free weeks: that reader may have had them, or been paying (L4).
    expect(document.body.textContent).not.toMatch(/free for your first two weeks|Two free weeks/);
    expect(screen.queryByText(/Talking with me is free/)).toBeNull();
    const stay = (event: Event) => event.preventDefault();
    window.addEventListener("click", stay);
    fireEvent.click(screen.getByRole("link", { name: "Sign in to start reading" }));
    window.removeEventListener("click", stay);
    expect(sessionStorage.getItem("mothli.account-deleted")).toBeNull();
  });

  // ADR 0045 step 7. Catches: a reader who asked to delete while paying not told their plan
  // won't renew and its day, told so with a counter, or offered no way to keep the plan;
  // "Keep my plan" keeping the plan of an account still set to be erased (charged with no
  // account), or not keeping the plan at all.
  describe("with a plan that won't renew", () => {
    const ENDS = "2099-12-03T10:00:00+00:00";
    const line = () => `Your plan won’t renew. It’s paid for until ${planDay(ENDS)}.`;
    beforeEach(() => mocks.planEnding.mockResolvedValue({ wont_renew: true, ends: ENDS }));

    test("the waiting screen says so with its day, beside Keep my plan", async () => {
      const { container } = render(<SignInGate session={pending}>{theApp()}</SignInGate>);
      expect(await screen.findByText(line())).toBeTruthy();
      expect(screen.getAllByRole("button").map((button) => button.textContent)).toEqual(
        ["Keep my account", "Keep my plan", "Sign out", "Take a copy of my data"]);
      expect(container.textContent).not.toMatch(/unlimited|questions? left|countdown|hours/i);
      expect(await axeAA(container)).toHaveNoViolations();
    });

    test("without a day, it still says the plan won't renew", async () => {
      mocks.planEnding.mockResolvedValue({ wont_renew: true, ends: null });
      render(<SignInGate session={pending}>{theApp()}</SignInGate>);
      expect(await screen.findByText("Your plan won’t renew.")).toBeTruthy();
    });

    test("Keep my plan keeps the account first, then the plan, then opens the app", async () => {
      const order: string[] = [];
      mocks.keepAccount.mockImplementation(async () => { order.push("account"); });
      mocks.keepPlan.mockImplementation(async () => { order.push("plan"); return {}; });
      render(<SignInGate session={pending}>{theApp()}</SignInGate>);
      fireEvent.click(await screen.findByRole("button", { name: "Keep my plan" }));
      await waitFor(() => expect(assign).toHaveBeenCalledWith("/?book=b1"));
      expect(order).toEqual(["account", "plan"]);
    });

    // Catches: a reader whose account is back kept on a screen that still says it is set to be
    // erased and still offers "Keep my account" (M1 of the plan walk, 23 November 2026).
    test("a plan that could not be kept: the account is back, so the shelf, with one calm line", async () => {
      mocks.keepPlan.mockRejectedValueOnce(new ApiError("busy", 503, "paddle_unavailable"));
      render(<SignInGate session={pending}>{theApp()}</SignInGate>);
      fireEvent.click(await screen.findByRole("button", { name: "Keep my plan" }));
      await waitFor(() => expect(assign).toHaveBeenCalledWith("/"));
      expect(mocks.keepAccount).toHaveBeenCalledTimes(1);
      expect(mocks.keepPlan).toHaveBeenCalledTimes(1);
      expect(screen.queryByRole("alert")).toBeNull();
      // The shelf, after the page opens again, says it once.
      const said = `Your account is back! I couldn’t keep your plan just now, so it still ends on ${planDay(ENDS)}. You can keep it from Your account in a little while.`;
      expect(keptWithoutPlan(planDay(ENDS))).toBe(said);
      expect(takeKeptWithoutPlan()).toBe(said);
      expect(takeKeptWithoutPlan()).toBeNull();
      noCounter(document.body);
    });

    test("without a day, the line says the plan still won't renew", async () => {
      mocks.planEnding.mockResolvedValue({ wont_renew: true, ends: null });
      mocks.keepPlan.mockRejectedValueOnce(new ApiError("busy", 409, "try_again_soon"));
      render(<SignInGate session={pending}>{theApp()}</SignInGate>);
      fireEvent.click(await screen.findByRole("button", { name: "Keep my plan" }));
      await waitFor(() => expect(assign).toHaveBeenCalledWith("/"));
      expect(takeKeptWithoutPlan()).toBe(
        "Your account is back! I couldn’t keep your plan just now, so it still won’t renew. You can keep it from Your account in a little while.");
    });

    test("an account that could not be kept leaves the plan alone", async () => {
      mocks.keepAccount.mockRejectedValueOnce(new TypeError("offline"));
      render(<SignInGate session={pending}>{theApp()}</SignInGate>);
      fireEvent.click(await screen.findByRole("button", { name: "Keep my plan" }));
      expect((await screen.findByRole("alert")).textContent).toBe(
        "That didn’t work just now. Check your connection and try again.");
      expect(mocks.keepPlan).not.toHaveBeenCalled();
      expect(assign).not.toHaveBeenCalled();
    });

    test("a plan that ended meanwhile has nothing to keep: the app opens", async () => {
      mocks.keepPlan.mockRejectedValueOnce(new ApiError("no plan", 409, "no_plan"));
      render(<SignInGate session={pending}>{theApp()}</SignInGate>);
      fireEvent.click(await screen.findByRole("button", { name: "Keep my plan" }));
      await waitFor(() => expect(assign).toHaveBeenCalledWith("/?book=b1"));
    });
  });

  test("no plan line or Keep my plan where the plan renews, none is managed, or the read fails", async () => {
    for (const answer of [() => mocks.planEnding.mockResolvedValue({ wont_renew: false, ends: null }),
      () => mocks.planEnding.mockRejectedValue(new ApiError("Not Found", 404)),
      () => mocks.planEnding.mockRejectedValue(new TypeError("offline"))]) {
      answer();
      render(<SignInGate session={pending}>{theApp()}</SignInGate>);
      await waitFor(() => expect(mocks.planEnding).toHaveBeenCalled());
      await act(() => new Promise((settled) => setTimeout(settled, 0)));
      expect(screen.queryByText(/won’t renew/)).toBeNull();
      expect(screen.queryByRole("button", { name: "Keep my plan" })).toBeNull();
      cleanup();
      mocks.planEnding.mockReset();
    }
  });

  // Catches: the erasure's day worked out in UTC beside a plan day in the reader's own zone (M3
  // of the plan walk), or a day named that comes after the erasure where the reader lives.
  test("the day is the reader's own, written as every plan screen writes one", () => {
    const now = new Date("2026-11-17T12:00:00Z");
    expect(erasureDay("2026-12-19T00:00:00+00:00", now, "UTC")).toBe("on Friday 18 December");
    expect(erasureDay("2027-01-03T00:00:00+00:00", now, "UTC")).toBe("on Saturday 2 January 2027");
    // East of UTC the erasure's first moment already falls on the 19th; west of it, the 18th.
    expect(erasureDay("2026-12-19T00:00:00+00:00", now, "Europe/Berlin")).toBe("on Saturday 19 December");
    expect(erasureDay("2026-12-19T00:00:00+00:00", now, "America/New_York")).toBe("on Friday 18 December");
    expect(erasureDay("not a date", now)).toBe("in 30 days");
  });

  test("the waiting screen writes both its days the same way, in the same zone", async () => {
    mocks.planEnding.mockResolvedValue({ wont_renew: true, ends: "2099-12-03T10:00:00+00:00" });
    render(<SignInGate session={pending}>{theApp()}</SignInGate>);
    const plan = await screen.findByText(/^Your plan won’t renew\. It’s paid for until /);
    expect(plan.textContent).toBe(`Your plan won’t renew. It’s paid for until ${planDay("2099-12-03T10:00:00+00:00")}.`);
    expect(plan.textContent).toMatch(/until \d{1,2} December 2099\.$/);
    expect(screen.getByRole("heading", { level: 1 }).textContent).toMatch(/on [A-Z][a-z]+day \d{1,2} December 2099\.$/);
  });
});

describe("SignIn", () => {
  test("says so when the device is offline", () => {
    const online = Object.getOwnPropertyDescriptor(Navigator.prototype, "onLine");
    Object.defineProperty(navigator, "onLine", { configurable: true, get: () => false });
    try {
      render(<SignIn />);
      expect(screen.getByRole("status").textContent).toBe(
        "You’re offline right now. Sign in once you’re back on the internet.",
      );
    } finally {
      delete (navigator as { onLine?: boolean }).onLine;
      if (online) Object.defineProperty(Navigator.prototype, "onLine", online);
    }
  });

  test("asks to come back only to a path inside the app", () => {
    expect(signInHref("/?book=b1")).toBe("/api/auth/login?return_to=%2F%3Fbook%3Db1");
    expect(signInHref("//elsewhere.example/")).toBe("/api/auth/login?return_to=%2F");
    expect(signInHref("https://elsewhere.example/")).toBe("/api/auth/login?return_to=%2F");
    expect(signInHref("/\\elsewhere.example")).toBe("/api/auth/login?return_to=%2F");
  });
});
