/** The public pages written out at build time (prerender.tsx): every version the server reads,
 * each one exactly what the app draws once its placeholders are filled in, and the app's first
 * draw over it changing nothing on screen. */
import { act } from "react";
import { createRoot } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { SignIn } from "../shelf/SignIn";
import { formatMinor } from "../ui/planFigures";
import { PAGE_PATHS, type PublicPageName } from "./pages";
import { PublicPage, forgetSite, knowSite } from "./PublicPage";
import { SLOT_PATTERN, moneyTable, prerender, type PageName } from "./prerender";
import { planShown, type CatalogPlan, type Site } from "./site";

const SHELL = `<!doctype html>
<html lang="en">
  <head>
    <meta name="description" content="A quiet space for your books." />
    <title>Mothli — A little company between the pages</title>
    <script type="module" crossorigin src="/assets/index-abc.js"></script>
  </head>
  <body>
    <div id="root"></div>
  </body>
</html>`;

// Characters that must be escaped, as a legal name may hold them.
const OPERATOR = { legal_name: `Smith & Jones "Books" Ltd`, country: "Ireland", contact_email: "hello@reader.example" };
const PLAN: CatalogPlan = { currency: "USD", monthly: "1299", yearly: "7999" };
const FLAT: CatalogPlan = { currency: "EUR", monthly: "1000", yearly: "12000" };
const NAV = ['<a href="/terms">Terms</a>', '<a href="/privacy">Privacy</a>', '<a href="/refunds">Refunds</a>', '<a href="/contact">Contact</a>'];

const escape = (value: string) =>
  value.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#x27;");

/** What the server does (backend/app/hosted/public_pages.py): each placeholder filled in from
 * the site's answer, escaped. */
function filled(document: string, site: Site): string {
  const shown = planShown(site.plan);
  const values: Record<string, string | undefined> = {
    legal_name: site.operator?.legal_name, country: site.operator?.country,
    contact_email: site.operator?.contact_email, writer: site.writer ?? undefined,
    monthly: shown?.monthly, yearly_total: shown?.yearlyTotal, yearly_per_month: shown?.yearlyPerMonth,
    saving: shown?.saving == null ? undefined : String(shown.saving),
  };
  return document.replace(SLOT_PATTERN, (_all, name: string) => {
    const value = values[name];
    if (value === undefined) throw new Error(`no value for ${name}`);
    return escape(value);
  });
}

const rootOf = (document: string) => document.split('<div id="root">')[1].split("</div>\n  </body>")[0];

/** What the app draws for the site's answer, as it draws it after the read. */
function appDraws(page: PageName, site: Site): string {
  knowSite(site);
  try {
    return renderToStaticMarkup(page === "front" ? <SignIn /> : <PublicPage page={page} />);
  } finally {
    forgetSite();
  }
}

beforeEach(() => {
  forgetSite();
  history.replaceState(null, "", "/");
});

afterEach(() => {
  forgetSite();
  vi.restoreAllMocks();
});

describe("the pages written out", () => {
  const built = prerender(SHELL);

  // Catches: a version the server reads missing, a placeholder it does not know, or a page
  // without the links to the others.
  test("every version is there, with the links and its own head", () => {
    expect(Object.fromEntries(Object.entries(built.pages).map(([page, versions]) => [page, Object.keys(versions).sort()])))
      .toEqual({
        front: ["o0p0", "o0p1", "o0p2", "o1p0", "o1p1", "o1p2"],
        terms: ["o0", "o1"], privacy: ["o0", "o1w0", "o1w1"], refunds: ["o0", "o1"], contact: ["o0", "o1"],
      });
    for (const [page, versions] of Object.entries(built.pages)) {
      for (const document of Object.values(versions)) {
        for (const link of NAV) expect(document).toContain(link);
        const path = page === "front" ? "/" : PAGE_PATHS[page as PublicPageName];
        expect(document).toContain(`<link rel="canonical" href="${path}" />`);
        expect(document).toContain('<script type="module" crossorigin src="/assets/index-abc.js"></script>');
        expect(document.match(/<title>/g)).toHaveLength(1);
        expect(document).not.toContain('<div id="root"></div>');
      }
    }
    expect(built.pages.terms.o1).toContain("<title>Terms of use · Mothli</title>");
    expect(built.pages.terms.o1).toContain('<meta name="description" content="These are the rules for using Mothli.');
    expect(built.pages.front.o1p1).toContain("<title>Mothli — A little company between the pages</title>");
    expect(built.shell).toBe(SHELL);
  });

  // Catches: the written-out words drifting from the app's: a second copy, a section left out,
  // a price written differently, a name not escaped as the app escapes it.
  test("each version filled in is exactly what the app draws", () => {
    const cases: [PageName, string, Site][] = [
      ["front", "o1p1", { operator: OPERATOR, plan: PLAN, writer: null }],
      ["front", "o1p2", { operator: OPERATOR, plan: FLAT, writer: null }],
      ["front", "o1p0", { operator: OPERATOR, plan: null, writer: null }],
      ["front", "o0p0", { operator: null, plan: null, writer: null }],
      ["front", "o0p1", { operator: null, plan: PLAN, writer: null }],
      ["privacy", "o1w1", { operator: OPERATOR, plan: null, writer: "OpenAI" }],
      ["privacy", "o1w0", { operator: OPERATOR, plan: null, writer: null }],
      ["privacy", "o0", { operator: null, plan: null, writer: null }],
    ];
    for (const page of ["terms", "refunds", "contact"] as const) {
      cases.push([page, "o1", { operator: OPERATOR, plan: null, writer: null }]);
      cases.push([page, "o0", { operator: null, plan: null, writer: null }]);
    }
    for (const [page, version, site] of cases) {
      const written = rootOf(filled(built.pages[page][version], site));
      expect(written, `${page} ${version}`).toBe(appDraws(page, site));
    }
    expect(rootOf(filled(built.pages.front.o1p1, { operator: OPERATOR, plan: PLAN, writer: null })))
      .toContain("$6.67 a month, billed yearly");
  });

  // Catches: the server told to write an amount differently from the app.
  test("the currency table writes amounts as the app does", () => {
    const table = moneyTable();
    expect(table.USD).toEqual(["$", "", 2]);
    expect(table.JPY?.[2]).toBe(0);
    for (const [currency, [before, after, digits]] of Object.entries(table)) {
      const scale = 10 ** digits;
      const whole = Math.floor(1299 / scale).toLocaleString("en");
      const written = digits ? `${whole}.${String(1299 % scale).padStart(digits, "0")}` : whole;
      expect(`${before}${written}${after}`, currency).toBe(formatMinor(1299, currency));
    }
    expect(built.checks.length).toBeGreaterThanOrEqual(5);
    for (const { plan, shown } of built.checks) expect(planShown(plan)).toEqual(shown);
  });
});

describe("the app taking over", () => {
  // Catches: the page flashing, jumping or doubling when the app draws over the written-out
  // page, or React complaining about it.
  test("the app's first draw over the written-out front page changes nothing", async () => {
    const site: Site = { operator: OPERATOR, plan: PLAN, writer: null };
    const written = rootOf(filled(prerender(SHELL).pages.front.o1p1, site));
    const container = document.createElement("div");
    container.innerHTML = written;
    document.body.append(container);
    // As the browser holds it (its own way of writing the same page back).
    const shown = container.innerHTML;
    expect(container.textContent).toContain("Mothli is run by Smith & Jones \"Books\" Ltd.");
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    const complaints = vi.spyOn(console, "error");
    knowSite(site);
    const root = createRoot(container);
    // The fetch the page makes is never needed: the answer is already known.
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("not asked"); }));
    await act(async () => root.render(<SignIn />));
    expect(container.innerHTML).toBe(shown);
    expect(complaints).not.toHaveBeenCalled();
    act(() => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = false;
  });
});
