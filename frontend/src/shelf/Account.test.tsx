/** The hosted account sheet: who is signed in, "What Mothli remembers" only where the server
 * keeps it, and a sign-out that says plainly what it clears. */
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { axeAA } from "../test-a11y";

const mocks = vi.hoisted(() => ({ recollections: vi.fn(), logout: vi.fn() }));
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

  test("Escape closes it", () => {
    const onClose = vi.fn();
    render(<Account session={session} onClose={onClose} />);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
