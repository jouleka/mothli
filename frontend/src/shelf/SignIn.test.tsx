/** The hosted welcome and the gate in front of the app: local mode is untouched, a signed-out
 * reader gets one way in, and a sign-in that ends mid-read brings them back to the same page. */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { announcePendingDeletion, announceSignedOut, type OfflineSession } from "../pwa/offline";
import { axeAA } from "../test-a11y";

const mocks = vi.hoisted(() => ({ keepAccount: vi.fn(), logout: vi.fn(), exportAccount: vi.fn() }));
vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: mocks,
}));
import { SignIn, SignInGate, erasureDay, noteAccountDeleted, signInHref } from "./SignIn";

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
    const door = screen.getByRole("link", { name: "Sign in to start reading" });
    expect(door.getAttribute("href")).toBe("/api/auth/login?return_to=%2F%3Fbook%3Db1");
    expect(screen.getAllByRole("link")).toHaveLength(1);
    expect(container.querySelector("form, input")).toBeNull();
    expect(await axeAA(container)).toHaveNoViolations();
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
    fireEvent.click(screen.getByRole("link", { name: "Sign in to start reading" }));
    window.removeEventListener("click", stay);
    first.unmount();
    render(<SignInGate session={null}>{theApp()}</SignInGate>);
    expect(screen.getByRole("status").textContent).toBe("Hmm, that didn’t quite work. Let’s try once more.");
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
    expect(heading.textContent).toBe("Your account is set to be erased on Thursday 17 December 2099.");
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
      "Done. You’re signed out everywhere, and your account will be erased on Thursday 17 December 2099 "
      + "unless you sign in again before then.");
    const stay = (event: Event) => event.preventDefault();
    window.addEventListener("click", stay);
    fireEvent.click(screen.getByRole("link", { name: "Sign in to start reading" }));
    window.removeEventListener("click", stay);
    expect(sessionStorage.getItem("mothli.account-deleted")).toBeNull();
  });

  test("the day is read in UTC and carries the year only when it is not this year", () => {
    const now = new Date("2026-11-17T12:00:00Z");
    expect(erasureDay("2026-12-19T00:00:00+00:00", now)).toBe("on Friday 18 December");
    expect(erasureDay("2027-01-03T00:00:00+00:00", now)).toBe("on Saturday 2 January 2027");
    expect(erasureDay("not a date", now)).toBe("in 30 days");
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
