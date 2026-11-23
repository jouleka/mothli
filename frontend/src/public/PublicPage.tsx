/** Hosted mode: the public pages a visitor and Paddle's reviewers read without signing in (ADR
 * 0045 step 8): the terms, privacy, refunds and contact pages at their own addresses (pages.ts),
 * the footer every signed-out page carries, and the front page's part about Mothli and the plan.
 * Everything comes from this app and its server: no script, font or image from anywhere else,
 * and Paddle's script never loads here. */
import { useCallback, useEffect, useState } from "react";
import { Button } from "../ui/Button";
import { MothliMark } from "../ui/MothliMark";
import { monthlyTotal, yearlyMonth, yearlyTotal } from "../ui/planFigures";
import { Contact, Privacy, Refunds, Terms, UPDATED } from "./legal";
import { PAGE_PATHS, type PublicPageName } from "./pages";
import { fetchSite, planShown, type Operator, type Site } from "./site";
import "../shelf/shelf-redesign.css";
import "./public.css";

export const NOT_OPEN =
  "Mothli isn’t open for sign-ups here yet. Who runs it and how to reach them aren’t published on this server, so its terms aren’t either. Please come back later.";
export const SITE_FAILED = "I couldn’t open this page just now. Check your connection and try again.";

export const TITLES: Record<PublicPageName, string> = {
  terms: "Terms of use",
  privacy: "Privacy",
  refunds: "Refunds",
  contact: "Contact",
};

let pending: Promise<Site> | null = null;

/** The public read, once a page load (a failure is asked again on retry). */
function loadSite(): Promise<Site> {
  pending ??= fetchSite().catch((error: unknown) => {
    pending = null;
    throw error;
  });
  return pending;
}

/** For tests: forget the read, so the next render asks again. */
export function forgetSite(): void {
  pending = null;
}

export function useSite(): { site: Site | null; failed: boolean; retry: () => void } {
  const [site, setSite] = useState<Site | null>(null);
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let live = true;
    setFailed(false);
    loadSite().then(
      (found) => live && setSite(found),
      () => live && setFailed(true),
    );
    return () => {
      live = false;
    };
  }, [attempt]);
  const retry = useCallback(() => setAttempt((count) => count + 1), []);
  return { site, failed, retry };
}

/** The footer on every signed-out page: the four pages, and who runs Mothli once known. Given
 * `site` (the front page), it also says when Mothli is not open for sign-ups here. */
export function SiteFooter({ operator, site }: { operator?: Operator | null; site?: Site | null }) {
  const known = operator ?? site?.operator ?? null;
  return (
    <footer className="pp-footer">
      <nav aria-label="About Mothli">
        <ul>
          <li><a href={PAGE_PATHS.terms}>Terms</a></li>
          <li><a href={PAGE_PATHS.privacy}>Privacy</a></li>
          <li><a href={PAGE_PATHS.refunds}>Refunds</a></li>
          <li><a href={PAGE_PATHS.contact}>Contact</a></li>
        </ul>
      </nav>
      {known && <p>Mothli is run by {known.legal_name}.</p>}
      {site && !site.operator && <p>{NOT_OPEN}</p>}
    </footer>
  );
}

/** The front page's part about Mothli: what it is, what is free, the plan and its two prices
 * (the yearly one first, as a month, with its saving), or the plan without figures when the
 * server could not read them from Paddle. */
export function AboutMothli({ site }: { site: Site | null }) {
  const shown = planShown(site?.plan ?? null);
  return (
    <section className="pp-about" aria-labelledby="about-title">
      <h2 id="about-title">A reading friend who never spoils the ending</h2>
      <ul className="pp-points">
        <li>
          <strong>Bring your books.</strong> Upload your own EPUBs, or pick a free classic from the
          library.
        </li>
        <li>
          <strong>Ask anything.</strong> Who was that again? What just happened? Mothli knows the
          book only as far as you’ve read it, so it can’t spoil what comes next.
        </li>
        <li>
          <strong>Keep your place.</strong> Your highlights, notes and bookmarks are kept for you,
          on every device.
        </li>
      </ul>

      <h2 id="plan-title">Free to read. Mothli is the plan.</h2>
      <p>
        Reading here is free, always: your books, your place, your highlights and the free library.
        Mothli, the chat in every book, who’s who and the story so far, is the plan.
      </p>
      <p className="pp-free"><strong>Two free weeks with Mothli, no card needed.</strong> They start the first time you use it.</p>
      {shown ? (
        <ul className="pp-prices" aria-labelledby="plan-title">
          <li className="pp-price pp-price--best">
            <h3>Yearly <span className="pp-tag">Best value</span></h3>
            <p className="pp-price-main">{yearlyMonth(shown.yearlyPerMonth)}</p>
            <p>{yearlyTotal(shown.yearlyTotal, shown.saving)}</p>
          </li>
          <li className="pp-price">
            <h3>Monthly</h3>
            <p className="pp-price-main">{monthlyTotal(shown.monthly)}</p>
          </li>
        </ul>
      ) : (
        <p>After that, the plan is paid monthly or yearly. You’ll see the prices when you choose.</p>
      )}
      <p className="pp-quiet">
        Cancel any time from Your account, and keep reading for free either way. Payments are
        handled by Paddle, who sells Mothli on our behalf.
        {shown && " Paddle’s checkout shows your exact price where you live, with any tax."}
      </p>
    </section>
  );
}

/** One public page at its own address, signed in or not. */
export function PublicPage({ page }: { page: PublicPageName }) {
  const { site, failed, retry } = useSite();
  const title = TITLES[page];
  useEffect(() => {
    document.title = `${title} · Mothli`;
  }, [title]);
  const operator = site?.operator ?? null;
  const Body = { terms: Terms, privacy: Privacy, refunds: Refunds, contact: Contact }[page];
  return (
    <div className="mothli-library pp-page">
      <a className="skip-link" href="#page-main">Skip to the page</a>
      <header className="ml-top">
        <a className="ml-brand" href="/" aria-label="Mothli home">
          <MothliMark className="ml-brand-mark" size={38} />
          <span>Mothli</span>
        </a>
      </header>
      <main id="page-main" className="pp-main" tabIndex={-1}>
        <article className="pp-article">
          <h1>{title}</h1>
          {failed ? (
            <div className="pp-problem">
              <p role="alert">{SITE_FAILED}</p>
              <Button variant="secondary" onClick={retry}>Try again</Button>
            </div>
          ) : site === null ? (
            <p role="status" aria-busy="true" className="pp-quiet">One moment…</p>
          ) : operator ? (
            <>
              {page !== "contact" && <p className="pp-updated">Last updated {UPDATED}</p>}
              <Body operator={operator} />
            </>
          ) : (
            <p className="pp-not-open">{NOT_OPEN}</p>
          )}
          <p className="pp-back"><a href="/">Back to Mothli</a></p>
        </article>
      </main>
      <SiteFooter operator={operator} />
    </div>
  );
}
