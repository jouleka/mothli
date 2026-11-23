/** Hosted mode: Paddle.js, which shows the plan's prices and opens Paddle's checkout over the page
 * (ADR 0045 step 4). It is fetched from Paddle's own address only when the plan sheet or the
 * payment-link page (`_ptxn`, below) asks for it, never on any other screen, and initialised once
 * a page, as Paddle's pages say:
 * developer.paddle.com/paddle-js/about/include-paddlejs (the address; "Always load Paddle.js
 * directly from https://cdn.paddle.com/"), /paddle-js/methods/paddle-environment-set (the sandbox,
 * set "before calling any other Paddle.js methods"), /paddle-js/methods/paddle-initialize (once a
 * page; the token and the event callback), /paddle-js/methods/paddle-pricepreview (prices in the
 * reader's currency, located by their address, with tax) and /paddle-js/methods/paddle-checkout-open
 * (a server-made transaction by its id, the email prefilled). No package: the script tag is one of
 * Paddle's two documented ways, and nothing else is needed. */
import type { PlanCheckout } from "../api";

export const PADDLE_SCRIPT = "https://cdn.paddle.com/paddle/v2/paddle.js";
// A script that has neither loaded nor failed by now is not coming (a stalled network).
const LOAD_TIMEOUT_MS = 20_000;

export type PaddleEvent = { name?: string; data?: unknown };
/** Paddle.js's price preview answer, as far as the plan sheet reads it. Amounts are strings in
 * the currency's smallest unit (developer.paddle.com/api-reference/pricing-preview/preview-prices). */
export type PricePreviewResult = {
  data?: {
    currencyCode?: string;
    details?: {
      lineItems?: {
        price?: { id?: string };
        totals?: { total?: string };
        formattedTotals?: { total?: string };
      }[];
    };
  };
};
export type CheckoutOptions = {
  transactionId: string;
  customer?: { email: string };
  settings: { displayMode: "overlay"; locale: string };
};
/** As much of Paddle.js as Mothli uses. */
export type PaddleLike = {
  Environment: { set: (environment: "sandbox") => void };
  Initialize: (options: { token: string; eventCallback?: (event: PaddleEvent) => void }) => void;
  PricePreview: (request: { items: { priceId: string; quantity: number }[] }) => Promise<PricePreviewResult>;
  Checkout: { open: (options: CheckoutOptions) => void };
};

declare global {
  interface Window {
    Paddle?: PaddleLike;
  }
}

let ready: Promise<PaddleLike> | null = null;
let listener: ((event: PaddleEvent) => void) | null = null;

/** Paddle.Initialize takes one event callback a page: it passes each event to whoever listens. */
export function listenToPaddle(listen: (event: PaddleEvent) => void): () => void {
  listener = listen;
  return () => {
    if (listener === listen) listener = null;
  };
}

function addScript(): Promise<PaddleLike> {
  return new Promise((resolve, reject) => {
    const script = document.createElement("script");
    const fail = () => {
      window.clearTimeout(timer);
      script.remove();
      reject(new Error("Paddle.js did not load"));
    };
    const timer = window.setTimeout(fail, LOAD_TIMEOUT_MS);
    script.src = PADDLE_SCRIPT;
    script.async = true;
    script.addEventListener("load", () => {
      window.clearTimeout(timer);
      if (window.Paddle) resolve(window.Paddle);
      else fail();
    });
    script.addEventListener("error", fail);
    document.head.appendChild(script);
  });
}

/** Paddle.js, fetched and initialised on the first call; a failure can be tried again. */
export function loadPaddle(setup: Pick<PlanCheckout, "environment" | "client_token">): Promise<PaddleLike> {
  ready ??= (window.Paddle ? Promise.resolve(window.Paddle) : addScript())
    .then((paddle) => {
      if (setup.environment === "sandbox") paddle.Environment.set("sandbox");
      paddle.Initialize({ token: setup.client_token, eventCallback: (event) => listener?.(event) });
      return paddle;
    })
    .catch((reason: unknown) => {
      ready = null;
      throw reason;
    });
  return ready;
}

/** Mothli's payment-link page (ADR 0045, "One Paddle account, several products"): every checkout
 * the server makes carries this app's front page as its `checkout.url`, and Paddle's emails about
 * that checkout link here with `_ptxn` set to the transaction's id
 * (developer.paddle.com/build/transactions/default-payment-link). The id, when the page was
 * opened with exactly one well-formed one; anything else is no payment link at all. */
const TRANSACTION = /^txn_[a-z0-9]{26}$/;

export function paymentLinkTransaction(search = window.location.search): string | null {
  const found = new URLSearchParams(search).getAll("_ptxn");
  return found.length === 1 && TRANSACTION.test(found[0]) ? found[0] : null;
}

/** Takes `_ptxn` out of the address once it has been read, before Paddle.js loads: the app opens
 * that checkout itself (Paddle.Checkout.open, which "takes priority over the query parameter"),
 * so Paddle.js never opens a second one, and a reload or a later visit opens none. */
export function forgetPaymentLink(): void {
  const params = new URLSearchParams(window.location.search);
  if (!params.has("_ptxn")) return;
  params.delete("_ptxn");
  const rest = params.toString();
  history.replaceState(history.state, "", `${window.location.pathname}${rest ? `?${rest}` : ""}`);
}

/** Tests only: as if the page had just loaded. */
export function forgetPaddle(): void {
  ready = null;
  listener = null;
  delete window.Paddle;
  for (const script of document.querySelectorAll(`script[src="${PADDLE_SCRIPT}"]`)) script.remove();
}

export type PlanPrices = {
  monthly: { total: string } | null;
  /** `perMonth` and `saving` (a whole percent, rounded down) are null when they cannot be
   * computed, or there is no saving. */
  yearly: { total: string; perMonth: string | null; saving: number | null } | null;
};

const WHOLE = /^\d{1,15}$/;

function formatter(currency: string): Intl.NumberFormat | null {
  try {
    return new Intl.NumberFormat("en", { style: "currency", currency });
  } catch {
    return null;
  }
}

/** The two plans' prices, as Paddle gives them for this reader (their currency and tax), with
 * the yearly plan as a month and its saving against paying monthly worked out from those two
 * figures alone. A plan Paddle gave no price for is null; an answer with none at all fails. */
export async function planPrices(
  paddle: PaddleLike,
  prices: PlanCheckout["prices"],
): Promise<PlanPrices> {
  const ids = [prices.monthly, prices.yearly].filter((id): id is string => Boolean(id));
  const answer = await paddle.PricePreview({ items: ids.map((priceId) => ({ priceId, quantity: 1 })) });
  const lines = answer?.data?.details?.lineItems ?? [];
  const line = (id: string | null) => {
    const found = id ? lines.find((item) => item?.price?.id === id) : undefined;
    const total = found?.totals?.total;
    const shown = found?.formattedTotals?.total;
    return typeof total === "string" && WHOLE.test(total) && typeof shown === "string" && shown
      ? { minor: Number(total), shown }
      : null;
  };
  const monthly = line(prices.monthly);
  const yearly = line(prices.yearly);
  if (!monthly && !yearly) throw new Error("Paddle gave no prices");
  let perMonth: string | null = null;
  let saving: number | null = null;
  if (yearly) {
    const format = formatter(answer.data?.currencyCode ?? "");
    if (format) {
      const digits = format.resolvedOptions().maximumFractionDigits ?? 2;
      perMonth = format.format(Math.round(yearly.minor / 12) / 10 ** digits);
    }
    if (monthly && monthly.minor > 0) {
      const percent = Math.floor(((monthly.minor * 12 - yearly.minor) * 100) / (monthly.minor * 12));
      saving = percent >= 1 ? percent : null;
    }
  }
  return {
    monthly: monthly && { total: monthly.shown },
    yearly: yearly && { total: yearly.shown, perMonth, saving },
  };
}
