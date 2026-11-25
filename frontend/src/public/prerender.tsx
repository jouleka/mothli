/** The public pages written out as plain HTML at build time (ADR 0045 step 8), so a visitor, a
 * search engine or a payment company's checker reads them without running any JavaScript.
 *
 * Each page is drawn here by the app's own components (the front page's welcome, the terms, the
 * privacy notice, the refunds and contact pages), exactly as the app draws it, into the built
 * index.html. What only the server knows (who runs Mothli, the AI company its chat uses, the
 * prices from Paddle's catalog) is drawn as a placeholder, `@@name@@`, which the server fills in
 * at each request (backend/app/hosted/public_pages.py). Each way a page can differ (operator
 * named or not, prices known or not, a saving or not, a chat company or not) is drawn once.
 *
 * The prices are worked out on the server from the catalog, so it is given how this app writes
 * an amount in each currency (`money`, from `formatMinor` itself) and some plans with the figures
 * this app shows for them (`checks`, from `planShown` itself); the server shows no price at all
 * unless its own figures agree with every one. Nothing here holds a word of its own. */
import type { ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { SignIn } from "../shelf/SignIn";
import { formatMinor } from "../ui/planFigures";
import { PAGE_PATHS, type PublicPageName } from "./pages";
import { PublicPage, ShownPlan, TITLES, forgetSite, knowSite } from "./PublicPage";
import { planShown, type CatalogPlan, type PlanShown, type Site } from "./site";

export const SLOTS = [
  "legal_name", "country", "contact_email", "writer",
  "monthly", "yearly_total", "yearly_per_month", "saving",
] as const;
export type Slot = (typeof SLOTS)[number];
const slot = (name: Slot) => `@@${name}@@`;
export const SLOT_PATTERN = /@@([a-z_]+)@@/g;

export type PageName = "front" | PublicPageName;

export type Prerendered = {
  version: 1;
  /** The built index.html as it is: the app's page with nothing in it. */
  shell: string;
  /** Each page's documents by the way they differ: `o1` operator named, `o0` not; on the front
   * page `p0` no prices, `p1` prices with a saving, `p2` prices without one; on the privacy page
   * `w1` the chat's AI company named, `w0` not. */
  pages: Record<PageName, Record<string, string>>;
  /** Per currency: what comes before the amount, what comes after, and its decimal places. */
  money: Record<string, [string, string, number]>;
  checks: { plan: CatalogPlan; shown: PlanShown }[];
};

const OPERATOR = { legal_name: slot("legal_name"), country: slot("country"), contact_email: slot("contact_email") };
const PRICED: PlanShown = {
  monthly: slot("monthly"), yearlyTotal: slot("yearly_total"), yearlyPerMonth: slot("yearly_per_month"),
  // Written into the page as text, like the number it stands for.
  saving: slot("saving") as unknown as number,
};

function drawn(site: Site, element: ReactElement, shown: PlanShown | null = null): string {
  knowSite(site);
  try {
    return renderToStaticMarkup(<ShownPlan.Provider value={shown}>{element}</ShownPlan.Provider>);
  } finally {
    forgetSite();
  }
}

const site = (named: boolean, writer: string | null = null): Site => ({
  operator: named ? OPERATOR : null, plan: null, writer,
});

/** Each page's markup, by the way it differs. */
export function markup(): Record<PageName, Record<string, string>> {
  const front: Record<string, string> = {};
  for (const named of [false, true]) {
    const o = named ? "o1" : "o0";
    front[`${o}p0`] = drawn(site(named), <SignIn />, null);
    front[`${o}p1`] = drawn(site(named), <SignIn />, PRICED);
    front[`${o}p2`] = drawn(site(named), <SignIn />, { ...PRICED, saving: null });
  }
  const pages = { front } as Record<PageName, Record<string, string>>;
  for (const page of Object.keys(PAGE_PATHS) as PublicPageName[]) {
    pages[page] = page === "privacy"
      ? {
        o0: drawn(site(false), <PublicPage page={page} />),
        o1w0: drawn(site(true), <PublicPage page={page} />),
        o1w1: drawn(site(true, slot("writer")), <PublicPage page={page} />),
      }
      : { o0: drawn(site(false), <PublicPage page={page} />), o1: drawn(site(true), <PublicPage page={page} />) };
  }
  return pages;
}

const ROOT = '<div id="root"></div>';

function once(text: string, part: string): void {
  const at = text.indexOf(part);
  if (at < 0 || text.indexOf(part, at + 1) >= 0) throw new Error(`index.html must hold ${part} once`);
}

/** A paragraph's text, from markup that is already escaped for HTML, for an attribute: its tags
 * taken out, and any "<" or ">" left over escaped, so nothing of it can be read as markup. */
function textOf(markup: string, className: string): string | null {
  const found = new RegExp(`<p class="${className}">([\\s\\S]*?)</p>`).exec(markup);
  if (!found) return null;
  return found[1].replace(/<[^>]*>/g, "").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/\s+/g, " ").trim();
}

/** The built index.html with a page's markup in its root, and the page's own title, description
 * and address in its head. */
export function documentOf(shell: string, page: PageName, markup: string): string {
  const title = page === "front"
    ? /<title>([\s\S]*?)<\/title>/.exec(shell)![1]
    : `${TITLES[page]} · Mothli`;
  const description = textOf(markup, page === "front" ? "ml-spread-lede" : "pp-lede")
    ?? /<meta name="description" content="([^"]*)"/.exec(shell)![1];
  const head = [
    `<link rel="canonical" href="${page === "front" ? "/" : PAGE_PATHS[page]}" />`,
    `<meta property="og:type" content="website" />`,
    `<meta property="og:title" content="${title}" />`,
    `<meta property="og:description" content="${description}" />`,
  ].join("\n    ");
  // Replaced by functions, so a "$" in the words is never read as a pattern.
  return shell
    .replace(/<title>[\s\S]*?<\/title>/, () => `<title>${title}</title>`)
    .replace(/(<meta name="description" content=")[^"]*(")/, (_all, before, after) => `${before}${description}${after}`)
    .replace("</head>", () => `  ${head}\n  </head>`)
    .replace(ROOT, () => `<div id="root">${markup}</div>`);
}

const SAMPLE = 123456789;

/** How this app writes amounts in each currency it knows, read from `formatMinor` itself: the
 * sample amount's digits, grouped in threes, with what stands before and after them. */
export function moneyTable(): Record<string, [string, string, number]> {
  const table: Record<string, [string, string, number]> = {};
  for (const currency of Intl.supportedValuesOf("currency")) {
    const written = formatMinor(SAMPLE, currency);
    if (!written) continue;
    const digits = new Intl.NumberFormat("en", { style: "currency", currency }).resolvedOptions()
      .maximumFractionDigits ?? 2;
    const scale = 10 ** digits;
    const whole = Math.floor(SAMPLE / scale).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    const amount = digits ? `${whole}.${String(SAMPLE % scale).padStart(digits, "0")}` : whole;
    const at = written.indexOf(amount);
    if (at < 0 || written.indexOf(amount, at + 1) >= 0) continue;
    table[currency] = [written.slice(0, at), written.slice(at + amount.length), digits];
  }
  return table;
}

/** Plans with the figures this app shows for them: rounding both ways, no saving, a currency
 * without decimals and one with three. */
const CHECKED: CatalogPlan[] = [
  { currency: "USD", monthly: "1299", yearly: "7999" },
  { currency: "EUR", monthly: "999", yearly: "9588" },
  { currency: "GBP", monthly: "1000", yearly: "12000" },
  { currency: "USD", monthly: "1000", yearly: "13000" },
  { currency: "USD", monthly: "1", yearly: "18" },
  { currency: "USD", monthly: "7", yearly: "90" },
  { currency: "JPY", monthly: "1200", yearly: "9999" },
  { currency: "KWD", monthly: "1500", yearly: "12345" },
  { currency: "USD", monthly: "123456789", yearly: "987654321" },
];

export function prerender(shell: string): Prerendered {
  once(shell, ROOT);
  once(shell, "</head>");
  const pages = markup();
  const known = new Set<string>(SLOTS);
  for (const [page, versions] of Object.entries(pages) as [PageName, Record<string, string>][]) {
    for (const [key, drawnMarkup] of Object.entries(versions)) {
      for (const [, name] of drawnMarkup.matchAll(SLOT_PATTERN)) {
        if (!known.has(name)) throw new Error(`${page} ${key}: unknown placeholder ${name}`);
      }
      versions[key] = documentOf(shell, page, drawnMarkup);
    }
  }
  const checks = CHECKED.flatMap((plan) => {
    const shown = planShown(plan);
    return shown ? [{ plan, shown }] : [];
  });
  return { version: 1, shell, pages, money: moneyTable(), checks };
}
