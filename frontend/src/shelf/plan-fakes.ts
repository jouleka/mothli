/** Test helpers for the plan screens: a stand-in for Paddle.js (`window.Paddle`) and its script
 * element, a plan as the server answers it, and the words no plan screen may hold. Nothing here
 * reaches Paddle. */
import { expect } from "vitest";
import type { Plan } from "../api";
import type { PaddleLike, PricePreviewResult } from "./paddle";

const CHECKOUT = {
  environment: "sandbox" as const,
  client_token: "test_clientstandin",
  prices: { monthly: "pri_monthlystandin", yearly: "pri_yearlystandin" },
};
export const planOf = (values: Partial<Plan> = {}): Plan => ({
  state: "free", why: null, plan: null, next_date: "2099-12-04T10:00:00+00:00", subscribed: false,
  cancel_scheduled: false, payment_retrying: false, manage: true, checkout: CHECKOUT, ...values,
});

/** Every line a reader can read in the sheet, for the words that must never appear. */
export function noCounter(element: Element): void {
  const text = element.textContent ?? "";
  expect(text).not.toMatch(/unlimited/i);
  expect(text).not.toMatch(/questions? left/i);
  expect(text).not.toMatch(/\d+\s*(questions?|replies|messages|chats?)\b/i);
  // Open point 8, decided: no refund promise of Mothli's own, Paddle's terms only. The one
  // mention allowed is a link to the refunds page (ADR 0045 step 8), which says so.
  const withoutLinks = element.cloneNode(true) as Element;
  withoutLinks.querySelectorAll('a[href="/refunds"]').forEach((link) => link.remove());
  expect(withoutLinks.textContent ?? "").not.toMatch(/refund|money.back|guarantee|\b14 days\b/i);
}


/** A stand-in for Paddle.js, recording what the app asks of it. */
export function fakePaddle(preview?: PricePreviewResult | Error): PaddleLike & {
  calls: string[]; emit: (name: string) => void; opened: unknown[];
} {
  let callback: ((event: { name?: string }) => void) | undefined;
  const calls: string[] = [];
  const opened: unknown[] = [];
  return {
    calls,
    opened,
    emit: (name) => callback?.({ name }),
    Environment: { set: (environment) => void calls.push(`environment:${environment}`) },
    Initialize: (options) => {
      calls.push(`initialize:${options.token}`);
      callback = options.eventCallback;
    },
    PricePreview: (request) => {
      calls.push(`preview:${request.items.map((item) => item.priceId).join(",")}`);
      return preview instanceof Error ? Promise.reject(preview) : Promise.resolve(preview ?? usd());
    },
    Checkout: { open: (options) => void opened.push(options) },
  };
}

/** Paddle's price preview, as Paddle.js answers it (camelCase), for amounts in minor units. */
export function preview(currencyCode: string, lines: [string, string, string][]): PricePreviewResult {
  return {
    data: {
      currencyCode,
      details: {
        lineItems: lines.map(([priceId, total, formatted]) => ({
          price: { id: priceId }, totals: { total }, formattedTotals: { total: formatted },
        })),
      },
    },
  };
}

export const usd = () => preview("USD", [
  ["pri_monthlystandin", "1299", "$12.99"],
  ["pri_yearlystandin", "7999", "$79.99"],
]);

export function paddleScripts(): HTMLScriptElement[] {
  return Array.from(document.querySelectorAll<HTMLScriptElement>("script")).filter((script) =>
    script.src.startsWith("https://cdn.paddle.com/"));
}

/** Answers the pending script as loaded (with `paddle` as window.Paddle) or as failed. */
export function settleScript(paddle: PaddleLike | null): void {
  const [script] = paddleScripts();
  if (paddle) {
    (window as unknown as { Paddle?: PaddleLike }).Paddle = paddle;
    script.dispatchEvent(new Event("load"));
  } else script.dispatchEvent(new Event("error"));
}
