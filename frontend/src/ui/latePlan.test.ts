/** The quiet watch for a payment that reaches Mothli after the plan sheet stopped waiting (H2 of
 * the plan walk, 23 November 2026). Nothing reaches Paddle; the server's plan is a mock. */
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

const mocks = vi.hoisted(() => ({ plan: vi.fn() }));
vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: mocks,
}));
import { planOf } from "../shelf/plan-fakes";
import {
  claimLatePlan,
  LATE_ASK_EVERY_MS,
  LATE_REPEAT_FOR_MS,
  LATE_WHEN_SHOWN_FOR_MS,
  stopWatchingForLatePlan,
  watchForLatePlan,
  watchingForLatePlan,
} from "./latePlan";
import { mothliStanding, setMothliStanding } from "./mothliStanding";

const ENDED = { state: "ended", why: "plan" };
const PAYING = planOf({ state: "paying", plan: "monthly", subscribed: true });

function showTab(state: "visible" | "hidden") {
  Object.defineProperty(document, "visibilityState", { value: state, configurable: true });
  document.dispatchEvent(new Event("visibilitychange"));
}

beforeEach(() => {
  vi.useFakeTimers();
  mocks.plan.mockReset().mockResolvedValue(planOf({ state: "ended", why: "plan", next_date: null }));
  setMothliStanding(ENDED);
});
afterEach(() => {
  stopWatchingForLatePlan();
  vi.useRealTimers();
  showTab("visible");
});

describe("the watch for a late payment", () => {
  // Catches: a payment that arrives after the sheet closed leaving every panel shut until a
  // reload, wherever the reader is by then.
  test("asks every so often and opens Mothli up the moment the plan arrives", async () => {
    watchForLatePlan();
    await vi.advanceTimersByTimeAsync(LATE_ASK_EVERY_MS * 4);
    expect(mocks.plan).toHaveBeenCalledTimes(4);
    expect(mothliStanding()?.state).toBe("ended");
    mocks.plan.mockResolvedValue(PAYING);
    await vi.advanceTimersByTimeAsync(LATE_ASK_EVERY_MS);
    expect(mothliStanding()?.state).toBe("paying");
    expect(watchingForLatePlan()).toBe(false);
    await vi.advanceTimersByTimeAsync(LATE_ASK_EVERY_MS * 4);
    expect(mocks.plan).toHaveBeenCalledTimes(5);
  });

  test("a passing failure is asked again", async () => {
    mocks.plan.mockRejectedValueOnce(new TypeError("offline")).mockResolvedValue(PAYING);
    watchForLatePlan();
    await vi.advanceTimersByTimeAsync(LATE_ASK_EVERY_MS * 2);
    expect(mothliStanding()?.state).toBe("paying");
  });

  // Catches: a watch with no end.
  test("the repeat stops after its time; then only a tab shown again asks, for a day; then nothing", async () => {
    watchForLatePlan();
    await vi.advanceTimersByTimeAsync(LATE_REPEAT_FOR_MS + LATE_ASK_EVERY_MS * 4);
    const asked = mocks.plan.mock.calls.length;
    expect(asked).toBe(LATE_REPEAT_FOR_MS / LATE_ASK_EVERY_MS);
    await vi.advanceTimersByTimeAsync(LATE_ASK_EVERY_MS * 40);
    expect(mocks.plan).toHaveBeenCalledTimes(asked);
    // The reader comes back to the tab: asked once.
    showTab("hidden");
    showTab("visible");
    await vi.advanceTimersByTimeAsync(0);
    expect(mocks.plan).toHaveBeenCalledTimes(asked + 1);
    mocks.plan.mockResolvedValue(PAYING);
    showTab("visible");
    await vi.advanceTimersByTimeAsync(0);
    expect(mothliStanding()?.state).toBe("paying");
    expect(watchingForLatePlan()).toBe(false);
  });

  test("a day after the repeat ends, a tab shown again stops the watch without asking", async () => {
    watchForLatePlan();
    await vi.advanceTimersByTimeAsync(LATE_REPEAT_FOR_MS + LATE_WHEN_SHOWN_FOR_MS);
    const asked = mocks.plan.mock.calls.length;
    showTab("visible");
    await vi.advanceTimersByTimeAsync(0);
    expect(mocks.plan).toHaveBeenCalledTimes(asked);
    expect(watchingForLatePlan()).toBe(false);
  });

  // Catches: the panel behind an open sheet swapped out under it (the sheet says the news).
  test("an open sheet that claims the news says it; the standing is left to it", async () => {
    const claim = vi.fn(() => true);
    const release = claimLatePlan(claim);
    mocks.plan.mockResolvedValue(PAYING);
    watchForLatePlan();
    await vi.advanceTimersByTimeAsync(LATE_ASK_EVERY_MS);
    expect(claim).toHaveBeenCalledWith(PAYING);
    expect(mothliStanding()?.state).toBe("ended");
    release();
  });
});
