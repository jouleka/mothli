/** The hosted welcome and the gate in front of the app: local mode is untouched, a signed-out
 * reader gets one way in, and a sign-in that ends mid-read brings them back to the same page. */
import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test } from "vitest";
import { announceSignedOut, type OfflineSession } from "../pwa/offline";
import { axeAA } from "../test-a11y";
import { SignIn, SignInGate, signInHref } from "./SignIn";

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
