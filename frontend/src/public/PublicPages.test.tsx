/** The public pages (ADR 0045 step 8): what a visitor and Paddle's reviewers read without signing
 * in. Each page at its own address and from the footer; contact one click from the front page;
 * the prices exactly as the server read them from Paddle's catalog, the yearly one first with its
 * saving, and no figure at all when there are none; the pages when the operator's details are
 * missing; never a counter, "unlimited", a refund promise or a countdown; and no script from
 * anywhere else. The server is stood in for by a fake `fetch`. */
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { axeAA } from "../test-a11y";
import { SignIn } from "../shelf/SignIn";
import { CHAT_NOT_OPEN, DAILY_RHYTHM, PADDLE_LINKS, SHARED_NOTES } from "./legal";
import { PAGE_PATHS, publicPageAt, routedPublicPage, type PublicPageName } from "./pages";
import { NOT_OPEN, PublicPage, SITE_FAILED, TITLES, forgetSite } from "./PublicPage";
import type { Site } from "./site";

const OPERATOR = { legal_name: "Example Reading Ltd", country: "Ireland", contact_email: "hello@reader.example" };
const PLAN = { currency: "USD", monthly: "1299", yearly: "7999" };
const PAGES = Object.keys(PAGE_PATHS) as PublicPageName[];
// Words the owner ruled out, on every public page: a counter, "unlimited", a refund promise of
// Mothli's own, a countdown, fake urgency.
const NEVER = /unlimited|questions? left|\b14 days\b|fourteen days|money[- ]back|guarantee|countdown|hurry|only today|limited time/i;

let calls: string[] = [];

function serve(answer: Site | "fail" | number) {
  calls = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    calls.push(String(url));
    if (answer === "fail") throw new TypeError("offline");
    if (typeof answer === "number") return { ok: false, status: answer, json: async () => ({}) };
    return { ok: true, status: 200, json: async () => answer };
  }));
}

beforeEach(() => {
  forgetSite();
  history.replaceState(null, "", "/");
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  history.replaceState(null, "", "/");
});

async function opened(page: PublicPageName) {
  const view = render(<PublicPage page={page} />);
  await screen.findByRole("heading", { level: 1, name: TITLES[page] });
  await waitFor(() => expect(screen.queryByText("One moment…")).toBeNull());
  return view;
}

describe("where each page is", () => {
  // Catches: a page at no address, the same page at two, or the pages showing in local mode.
  test("each page has its own address, in hosted mode only, signed in or out", () => {
    expect(PAGE_PATHS).toEqual({ terms: "/terms", privacy: "/privacy", refunds: "/refunds", contact: "/contact" });
    for (const page of PAGES) {
      expect(routedPublicPage(null, PAGE_PATHS[page])).toBe(page);
      expect(routedPublicPage({ user: {} }, PAGE_PATHS[page])).toBe(page);
      expect(routedPublicPage(undefined, PAGE_PATHS[page])).toBeNull();
    }
    for (const path of ["/", "/terms/x", "/Terms", "/api/site", "/?terms"]) {
      expect(publicPageAt(path)).toBeNull();
    }
  });

  // Catches: a footer link missing, or one leading to an address that is not its page.
  test("every page is reached from the footer, on every page", async () => {
    serve({ operator: OPERATOR, plan: null, writer: null });
    for (const page of PAGES) {
      await opened(page);
      const footer = screen.getByRole("navigation", { name: "About Mothli" });
      const links = within(footer).getAllByRole("link");
      expect(links.map((link) => [link.textContent, link.getAttribute("href")])).toEqual([
        ["Terms", "/terms"], ["Privacy", "/privacy"], ["Refunds", "/refunds"], ["Contact", "/contact"],
      ]);
      for (const link of links) expect(routedPublicPage(null, link.getAttribute("href")!)).not.toBeNull();
      expect(screen.getByRole("link", { name: "Back to Mothli" }).getAttribute("href")).toBe("/");
      cleanup();
    }
  });

  // Catches (Paddle: contact details reachable from the home page): contact more than one click
  // from the signed-out front page, or the contact page without the address.
  test("contact is one click from the front page, and its page gives the address", async () => {
    serve({ operator: OPERATOR, plan: PLAN, writer: null });
    render(<SignIn />);
    const footer = await screen.findByRole("navigation", { name: "About Mothli" });
    const contact = within(footer).getByRole("link", { name: "Contact" });
    expect(routedPublicPage(null, contact.getAttribute("href")!)).toBe("contact");
    await screen.findByText("Mothli is run by Example Reading Ltd.");
    cleanup();
    await opened("contact");
    const mail = screen.getAllByRole("link", { name: OPERATOR.contact_email })[0];
    expect(mail.getAttribute("href")).toBe(`mailto:${OPERATOR.contact_email}`);
    expect(screen.getByText("Example Reading Ltd, based in Ireland.")).toBeTruthy();
  });
});

describe("the pages' words", () => {
  // Catches: the terms without the seller's legal name (Paddle's review), a page with the
  // operator's details missing, or a page failing accessibility.
  test("each page names who runs Mothli and passes the accessibility checks", async () => {
    serve({ operator: OPERATOR, plan: null, writer: null });
    for (const page of PAGES) {
      const { container } = await opened(page);
      expect(container.textContent).toContain("Example Reading Ltd");
      expect(container.textContent).toContain(OPERATOR.contact_email);
      expect(container.querySelector("main h1")).not.toBeNull();
      expect(await axeAA(container)).toHaveNoViolations();
      cleanup();
    }
  });

  // Catches (the owner's rules): a counter, "unlimited", "14 days", a money-back promise, a
  // guarantee or a countdown on any public page or the front page.
  test("no page says a number of questions, unlimited, a refund promise or a countdown", async () => {
    serve({ operator: OPERATOR, plan: PLAN, writer: null });
    for (const page of PAGES) {
      const { container } = await opened(page);
      expect(container.textContent).not.toMatch(NEVER);
      cleanup();
    }
    const { container } = render(<SignIn />);
    await screen.findByText("$12.99 a month");
    expect(container.textContent).not.toMatch(NEVER);
    expect(container.textContent).not.toMatch(/\bquestions?\b.*\d|\d.*\bquestions?\b/i);
  });

  // Catches (the owner's decision on refunds): a refund page that promises anything of Mothli's
  // own instead of pointing to Paddle's terms, forgets cancelling, or still offers the free weeks
  // removed by ADR 0046 ("Try it first").
  test("the refunds page is Paddle's terms and cancelling any time, and no free weeks", async () => {
    serve({ operator: OPERATOR, plan: null, writer: null });
    const { container } = await opened("refunds");
    const text = container.textContent ?? "";
    expect(text).toContain("refunds are handled by Paddle, under Paddle’s own terms");
    expect(text).toContain("You keep Mothli until the end of the month or year you have paid for");
    expect(text).not.toMatch(/Try it first|free weeks|first two weeks|don’t need a card/);
    expect(text).toContain("the right of withdrawal for buyers in the European Union and the United Kingdom, Paddle’s terms explain");
    expect(screen.getByRole("link", { name: "Paddle’s refund policy" }).getAttribute("href")).toBe(PADDLE_LINKS.refundPolicy);
    expect(screen.getAllByRole("link", { name: "Paddle’s buyer terms" })[0].getAttribute("href")).toBe(PADDLE_LINKS.buyerTerms);
    expect(text).not.toMatch(/\bwe (will|’ll|'ll) refund|refund (it|you) in full|no questions asked/i);
  });

  // Catches: the terms or privacy page missing what is true and owed to readers: Paddle as the
  // seller, AI answers can be wrong, the shared notes sentence, the providers by kind, the 30
  // days, the backups and Paddle's own records after erasure, cookies only for signing in.
  test("the terms and the privacy page say what Mothli really does", async () => {
    serve({ operator: OPERATOR, plan: null, writer: "OpenAI" });
    let text = (await opened("terms")).container.textContent ?? "";
    for (const said of [
      "Mothli is run by Example Reading Ltd, based in Ireland",
      "Paddle sells the plan to you on our behalf",
      "They can be wrong",
      "Only upload books you have the right to use",
      "governed by the law of Ireland",
      // Step 7: asking to delete stops the plan renewing by itself; never "cancel first".
      "If you’re on the plan, asking to delete your account stops it from renewing, so you won’t be charged again. You keep Mothli until the end of the month or year you have paid for, or until your account is erased, whichever comes first. If you come back within the 30 days, your plan still ends unless you choose “Keep my plan”.",
      // ADR 0046: talking with Mothli is the plan, with no free weeks.
      "Talking with Mothli is the plan: in your books and on your home page, along with who’s who and the story so far. The plan is paid every month or every year. The prices are on our front page and in the checkout.",
      SHARED_NOTES,
      // The owner's soft brake (27 November 2026), in words, never a number.
      "Mothli keeps a gentle daily rhythm, so on a very busy day it may ask to pick up again tomorrow. The plan also has fair-use limits for each day and each month",
    ]) expect(text).toContain(said);
    expect(text).not.toMatch(/cancel it[^.]*before you delete/i);
    expect(text).not.toMatch(/free weeks|first two weeks|don’t need a card/);
    cleanup();
    text = (await opened("privacy")).container.textContent ?? "";
    for (const said of [
      "OpenAI, whose AI model writes Mothli’s replies and notes, and TypeSafe",
      "Cloudflare", "The sign-in service", "Paddle",
      "We never see or keep your card details",
      "we keep everything for 30 days",
      "for up to two weeks after that",
      "keeps its own records of your purchases and payments, even after we erase your account. We don’t ask Paddle to delete them.",
      "deleting your account stops it from renewing",
      "and your plan (any gift, and your plan’s status and dates)",
      "The books themselves aren’t in it: open a book, press Notebook, and choose “Download the book”.",
      "Mothli sets three cookies, all needed for signing in",
      "no advertising, analytics or tracking cookies",
      SHARED_NOTES,
    ]) expect(text).toContain(said);
    expect(text).not.toContain("Anthropic");
    expect(text).not.toMatch(/free weeks/);
  });

  // Catches: the privacy page offering two possibilities, or naming a company the server does
  // not send to; or naming any AI company where the chat is not open on this server.
  test("the privacy page names the one AI company this server uses, or none", async () => {
    serve({ operator: OPERATOR, plan: null, writer: "Anthropic" });
    let text = (await opened("privacy")).container.textContent ?? "";
    expect(text).toContain("Anthropic, whose AI model writes Mothli’s replies and notes, and TypeSafe");
    expect(text).not.toContain("OpenAI");
    cleanup();
    forgetSite();
    serve({ operator: OPERATOR, plan: null, writer: null });
    text = (await opened("privacy")).container.textContent ?? "";
    expect(text).toContain(CHAT_NOT_OPEN);
    expect(text).not.toMatch(/OpenAI|Anthropic|TypeSafe/);
  });
});

describe("the operator's details missing", () => {
  // Catches: terms published with holes (no name, no country, no address) where the server has
  // not been given them.
  test("every page says Mothli is not open for sign-ups here, and nothing else", async () => {
    serve({ operator: null, plan: null, writer: null });
    for (const page of PAGES) {
      const { container } = await opened(page);
      expect(screen.getByText(NOT_OPEN)).toBeTruthy();
      expect(container.querySelectorAll("main h2")).toHaveLength(0);
      expect(container.textContent).not.toMatch(/Last updated|based in|Paddle/);
      cleanup();
    }
    render(<SignIn />);
    const footer = await screen.findByRole("contentinfo");
    await within(footer).findByText(NOT_OPEN);
  });

  // Catches: a page left loading for ever, or blank, when the server cannot be reached.
  test("a page the server cannot answer says so and tries again", async () => {
    serve("fail");
    render(<PublicPage page="terms" />);
    await screen.findByText(SITE_FAILED);
    serve({ operator: OPERATOR, plan: null, writer: null });
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    await screen.findByText(/Mothli is run by Example Reading Ltd, based in Ireland/);
  });
});

describe("the front page", () => {
  // Catches: prices other than the catalog's, the monthly plan first, the saving or the month
  // worked out wrong or from anything but the two prices; or the removed free weeks (ADR 0046).
  test("the prices are the server's, the yearly plan first as a month with its saving", async () => {
    serve({ operator: OPERATOR, plan: PLAN, writer: null });
    const { container } = render(<SignIn />);
    const yearly = await screen.findByText("$6.67 a month, billed $79.99 a year");
    const total = screen.getByText("48% less than paying monthly.");
    const monthly = screen.getByText("$12.99 a month");
    expect(yearly.compareDocumentPosition(monthly) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(total.compareDocumentPosition(monthly) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(container.textContent).not.toMatch(/free weeks|no card needed|first time you use it/);
    expect(container.textContent).toContain("Payments are handled by Paddle, who sells Mothli on our behalf.");
    // The daily rhythm beside the prices, in words (the owner's soft brake, 27 November 2026).
    expect(screen.getByText(DAILY_RHYTHM, { exact: false }).closest(".pp-about")).toBeTruthy();
    expect(DAILY_RHYTHM).not.toMatch(/\d/);
    // A way in right beside the prices too (L9 of the plan walk), the same as the welcome's.
    const [door, again] = screen.getAllByRole("link", { name: "Sign in to start reading" });
    expect(again.getAttribute("href")).toBe(door.getAttribute("href"));
    expect(monthly.compareDocumentPosition(again) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(again.closest(".pp-about")).toBeTruthy();
    expect(calls).toEqual(["/api/site"]);
    expect(await axeAA(container)).toHaveNoViolations();
  });

  // Catches: another catalog's figures shown wrong (each figure follows the server's amounts and
  // currency; nothing is written in the app).
  test("other figures from the catalog are shown as they are", async () => {
    serve({ operator: OPERATOR, plan: { currency: "EUR", monthly: "1000", yearly: "9000" }, writer: null });
    render(<SignIn />);
    await screen.findByText("€7.50 a month, billed €90.00 a year");
    expect(screen.getByText("25% less than paying monthly.")).toBeTruthy();
    expect(screen.getByText("€10.00 a month")).toBeTruthy();
  });

  // Catches: a wrong or stale number where Paddle could not be read, or is not set up here, or
  // the server could not be reached: the plan is shown without any figure.
  test("with no prices from the server there is no figure at all", async () => {
    for (const answer of [{ operator: OPERATOR, plan: null, writer: null }, "fail", 503] as const) {
      forgetSite();
      serve(answer);
      const { container } = render(<SignIn />);
      await waitFor(() => expect(calls).toEqual(["/api/site"]));
      await screen.findByText("The plan is paid monthly or yearly. You’ll see the prices when you choose.");
      const about = container.querySelector(".pp-about")!;
      expect(about.textContent).not.toMatch(/\d|\$|€|£/);
      cleanup();
    }
  });

  // Catches (the security policy must not widen): Paddle's script, or any script, font, image or
  // stylesheet from another site, loaded on the front page or a public page.
  test("nothing on the public pages is loaded from another site", async () => {
    serve({ operator: OPERATOR, plan: PLAN, writer: null });
    const { container } = render(<SignIn />);
    await screen.findByText("$12.99 a month");
    for (const page of PAGES) {
      render(<PublicPage page={page} />);
      await screen.findByRole("heading", { level: 1, name: TITLES[page] });
    }
    await waitFor(() => expect(screen.queryAllByText("One moment…")).toHaveLength(0));
    const outside = [...document.querySelectorAll("script[src], link[href], img[src], iframe[src]")]
      .map((node) => node.getAttribute("src") ?? node.getAttribute("href") ?? "")
      .filter((address) => /^https?:|^\/\//.test(address));
    expect(outside).toEqual([]);
    expect(document.querySelector('script[src*="paddle"]')).toBeNull();
    expect(calls.every((url) => url === "/api/site")).toBe(true);
    expect(container).toBeTruthy();
  });
});
