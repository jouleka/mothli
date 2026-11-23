/** Paddle.js, loaded only when the plan sheet asks for it, and the prices read from it. Paddle.js
 * is stood in by a fake `window.Paddle`; nothing reaches Paddle. */
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import type { PlanCheckout } from "../api";
import {
  forgetPaddle,
  listenToPaddle,
  loadPaddle,
  PADDLE_SCRIPT,
  planPrices,
  type PricePreviewResult,
} from "./paddle";
import { fakePaddle, paddleScripts, preview, settleScript } from "./plan-fakes";

const SETUP: PlanCheckout = {
  environment: "sandbox",
  client_token: "test_clientstandin",
  prices: { monthly: "pri_monthlystandin", yearly: "pri_yearlystandin" },
};

beforeEach(() => forgetPaddle());
afterEach(() => {
  forgetPaddle();
  vi.useRealTimers();
});

describe("loading Paddle.js", () => {
  // Catches: Paddle's script on every page (it is fetched only when the plan sheet asks), from
  // anywhere but Paddle's own address, or added twice.
  test("adds Paddle's own script once, only when asked", async () => {
    expect(paddleScripts()).toEqual([]);
    const first = loadPaddle(SETUP);
    const second = loadPaddle(SETUP);
    expect(paddleScripts().map((script) => script.src)).toEqual([PADDLE_SCRIPT]);
    expect(PADDLE_SCRIPT).toBe("https://cdn.paddle.com/paddle/v2/paddle.js");
    const paddle = fakePaddle();
    settleScript(paddle);
    expect(await first).toBe(paddle);
    expect(await second).toBe(paddle);
    expect(await loadPaddle(SETUP)).toBe(paddle);
    // Initialised once, in the sandbox before anything else, as Paddle's pages say.
    expect(paddle.calls).toEqual(["environment:sandbox", "initialize:test_clientstandin"]);
    expect(paddleScripts()).toHaveLength(1);
  });

  test("live Paddle is the default: no environment is set", async () => {
    const loading = loadPaddle({ ...SETUP, environment: "production", client_token: "live_standin" });
    const paddle = fakePaddle();
    settleScript(paddle);
    await loading;
    expect(paddle.calls).toEqual(["initialize:live_standin"]);
  });

  // Catches: a blocked or offline script that leaves the sheet waiting for ever, or that can
  // never be tried again.
  test("a script that cannot load fails, and can be tried again", async () => {
    const loading = loadPaddle(SETUP);
    settleScript(null);
    await expect(loading).rejects.toThrow();
    expect(paddleScripts()).toEqual([]);
    const again = loadPaddle(SETUP);
    expect(paddleScripts()).toHaveLength(1);
    settleScript(fakePaddle());
    await expect(again).resolves.toBeTruthy();
  });

  test("a script that never answers gives up", async () => {
    vi.useFakeTimers();
    const loading = loadPaddle(SETUP);
    const failed = expect(loading).rejects.toThrow();
    await vi.advanceTimersByTimeAsync(20_000);
    await failed;
    expect(paddleScripts()).toEqual([]);
  });

  test("Paddle's events reach whoever listens now, and no one after", async () => {
    const loading = loadPaddle(SETUP);
    const paddle = fakePaddle();
    settleScript(paddle);
    await loading;
    const heard: string[] = [];
    const stop = listenToPaddle((event) => heard.push(event.name ?? ""));
    paddle.emit("checkout.completed");
    stop();
    paddle.emit("checkout.closed");
    expect(heard).toEqual(["checkout.completed"]);
  });
});

describe("the prices, from Paddle", () => {
  // Catches: a price written in the app; the monthly equivalent or the saving computed wrongly,
  // rounded up, or shown when it cannot be computed.
  test("the yearly plan as a month, and its honest saving rounded down", async () => {
    const paddle = fakePaddle();
    expect(await planPrices(paddle, SETUP.prices)).toEqual({
      monthly: { total: "$12.99" },
      yearly: { total: "$79.99", perMonth: "$6.67", saving: 48 },
    });
    expect(paddle.calls).toEqual(["preview:pri_monthlystandin,pri_yearlystandin"]);
  });

  test("in the reader's own currency, whatever its minor unit", async () => {
    const eur = fakePaddle(preview("EUR", [
      ["pri_monthlystandin", "1499", "€14.99"], ["pri_yearlystandin", "8999", "€89.99"]]));
    expect((await planPrices(eur, SETUP.prices)).yearly).toEqual(
      { total: "€89.99", perMonth: "€7.50", saving: 49 });
    const yen = fakePaddle(preview("JPY", [
      ["pri_monthlystandin", "1500", "¥1,500"], ["pri_yearlystandin", "12000", "¥12,000"]]));
    expect((await planPrices(yen, SETUP.prices)).yearly).toEqual(
      { total: "¥12,000", perMonth: "¥1,000", saving: 33 });
  });

  test("no saving where none can be computed or there is none", async () => {
    const onlyYearly = await planPrices(fakePaddle(preview("USD", [["pri_yearlystandin", "7999", "$79.99"]])),
      { monthly: null, yearly: "pri_yearlystandin" });
    expect(onlyYearly).toEqual({ monthly: null, yearly: { total: "$79.99", perMonth: "$6.67", saving: null } });
    const dearer = await planPrices(fakePaddle(preview("USD", [
      ["pri_monthlystandin", "500", "$5.00"], ["pri_yearlystandin", "7999", "$79.99"]])), SETUP.prices);
    expect(dearer.yearly?.saving).toBeNull();
    // A currency the browser cannot format: the saving is a ratio, the month is not shown.
    const strange = await planPrices(fakePaddle(preview("??", [
      ["pri_monthlystandin", "1299", "12.99"], ["pri_yearlystandin", "7999", "79.99"]])), SETUP.prices);
    expect(strange.yearly).toEqual({ total: "79.99", perMonth: null, saving: 48 });
  });

  // Catches: a plan offered with no price, or a price made up when Paddle's answer is odd.
  test("a plan Paddle gave no price for is not offered; an answer with none fails", async () => {
    const half = await planPrices(fakePaddle(preview("USD", [["pri_monthlystandin", "1299", "$12.99"]])), SETUP.prices);
    expect(half).toEqual({ monthly: { total: "$12.99" }, yearly: null });
    await expect(planPrices(fakePaddle(preview("USD", [])), SETUP.prices)).rejects.toThrow();
    await expect(planPrices(fakePaddle({ data: {} } as PricePreviewResult), SETUP.prices)).rejects.toThrow();
    await expect(planPrices(fakePaddle(new Error("offline")), SETUP.prices)).rejects.toThrow();
  });
});
