/** Hosted mode: the front page a first-time visitor sees (the welcome back after being signed
 * out, a payment link's checkout and the goodbye after deleting an account keep the plain
 * welcome in SignIn.tsx). It reads as one page of a book with Mothli living in its margin:
 * the moth flies in, lands beside each part, and writes its notes in blue pencil.
 *
 * Everything on it is drawn the same with or without JavaScript (the server writes it out
 * first, public/prerender.tsx), and the app's first draw over it changes nothing: the flying
 * moth and the lamp are drawn outside the page, and anything that moves waits for a tap. */
import { type ReactNode, useEffect, useLayoutEffect, useState } from "react";
import { Moth } from "../../ui/Moth";
import { MothliMark } from "../../ui/MothliMark";
import { AboutMothli, SiteFooter } from "../PublicPage";
import type { Site } from "../site";
import { shownFor } from "./arrival";
import { BookmarkDemo } from "./BookmarkDemo";
import { Flyer } from "./Flyer";
import { MarkALine } from "./MarkALine";
import { ShelfDemo } from "./ShelfDemo";
import "./landing.css";

// Layout effects in the browser only: the build draws this page without one.
const useBrowserLayoutEffect = typeof document !== "undefined" && !import.meta.env.SSR
  ? useLayoutEffect
  : useEffect;

const THEME_KEY = "mothli.theme";

export function Landing({ door, notes, site }: { door: ReactNode; notes: ReactNode; site: Site | null }) {
  const [lamp, setLamp] = useState(false);

  // Before the first paint: the opening's drawing-in carries on from where the written-out page
  // had got to (arrival.ts), and the drawn moth in the margin gives way to the flying one.
  useBrowserLayoutEffect(() => {
    const root = document.documentElement;
    root.style.setProperty("--lp-shift", `${-Math.round(shownFor())}ms`);
    root.classList.add("lp-live");
    return () => {
      root.classList.remove("lp-live", "lp-lamp");
      root.style.removeProperty("--lp-shift");
    };
  }, []);

  useEffect(() => {
    setLamp(document.documentElement.dataset.theme === "dark");
  }, []);

  useEffect(() => {
    document.documentElement.classList.toggle("lp-lamp", lamp);
  }, [lamp]);

  const switchLights = () => {
    const off = !lamp;
    setLamp(off);
    document.documentElement.dataset.theme = off ? "dark" : "light";
    try {
      localStorage.setItem(THEME_KEY, off ? "dark" : "light");
    } catch {
      /* optional: the page still switches */
    }
  };

  return (
    <div className="lp">
      <header className="lp-top">
        <span className="lp-brand">
          <MothliMark size={34} />
          <span>Mothli</span>
        </span>
        <button type="button" className="lp-lights" onClick={switchLights}>
          <span className="lp-lights-bulb" aria-hidden="true" />
          {lamp ? "Lights on" : "Lights off"}
        </button>
      </header>

      <main className="lp-main">
        <section className="lp-sheet lp-hero" aria-labelledby="welcome-title">
          <div className="lp-text">
            <h1 id="welcome-title" className="lp-title">Read with a friend in the margin.</h1>
            <p className="ml-spread-lede lp-lede lp-in lp-in--1">
              Bring a book you love. Mothli reads along with you and only knows it as far as you’ve
              read, so it can never spoil what happens next.
            </p>
            <div className="lp-door lp-in lp-in--3">
              {notes}
              {door}
              <p id="welcome-door" className="lp-small">
                You’ll sign in on a secure page of its own. Mothli never sees your password.
              </p>
            </div>
          </div>
          <div className="lp-margin lp-hero-margin">
            <svg className="lp-rule" viewBox="0 0 8 400" preserveAspectRatio="none" aria-hidden="true" focusable="false">
              <path d="M4 0C2 50 6 90 4 150S2 260 5 320 3 380 4 400" pathLength={1} />
            </svg>
            <span className="lp-hero-moth" data-perch="hero">
              <Moth size={64} />
            </span>
            <div className="m-hand lp-hero-note">
              <p className="lp-pencil lp-pencil--1">Hi, I’m Mothli.</p>
              <p className="lp-pencil lp-pencil--2">Lost track of who someone is? Just ask me.</p>
              <p className="lp-pencil lp-pencil--3">I only know the pages you’ve read, so no spoilers. Promise.</p>
            </div>
            <svg className="lp-scribble" viewBox="0 0 120 70" aria-hidden="true" focusable="false">
              <path d="M110 6C96 30 70 52 22 50" pathLength={1} />
              <path d="M34 38 20 50l15 10" pathLength={1} />
            </svg>
          </div>
          <p className="m-hand lp-onward">Scroll on, I’ll show you around.</p>
        </section>

        <section className="lp-sheet lp-sheet--book" aria-labelledby="lp-bookmark-title">
          <div className="lp-text">
            <h2 id="lp-bookmark-title" className="lp-heading">Ask anything. Hear nothing you haven’t read.</h2>
            <p className="lp-lede">
              Here’s Pride and Prejudice. Move the ribbon to where you’ve read up to, then ask
              Mothli something. What it knows moves with your bookmark.
            </p>
          </div>
          <div className="lp-margin">
            <p className="m-hand lp-aside">Try asking how it ends. Go on, I dare you.</p>
          </div>
          <BookmarkDemo />
        </section>

        <section className="lp-sheet lp-sheet--mark" aria-labelledby="lp-mark-title">
          <div className="lp-text">
            <h2 id="lp-mark-title" className="lp-heading">Mark a line. Say what you think.</h2>
            <p className="lp-lede">
              Highlight whatever makes you laugh, wince or wonder, and react in your own words.
              Mothli answers the way a friend would, right there in the margin. Tap a marked line
              to try it.
            </p>
          </div>
          <MarkALine />
        </section>

        <section className="lp-sheet lp-sheet--shelf" aria-labelledby="lp-shelf-title">
          <div className="lp-text">
            <h2 id="lp-shelf-title" className="lp-heading">Your books, on your shelf.</h2>
            <p className="lp-lede">
              Upload your own EPUBs, or borrow a classic from the free library. Your place, your
              highlights and your notes follow you to every device.
            </p>
          </div>
          <ShelfDemo />
        </section>

        <section className="lp-sheet lp-sheet--plan" aria-label="Mothli and the plan">
          <div className="lp-text">
            <AboutMothli site={site} action={door} />
          </div>
          <div className="lp-margin">
            <span className="lp-perch lp-perch--plan" data-perch="plan" aria-hidden="true" />
            <p className="m-hand lp-aside">The small print, in big print. No surprises.</p>
          </div>
        </section>
      </main>

      <div className="lp-foot">
        <SiteFooter site={site} />
      </div>
      <Flyer lamp={lamp} />
    </div>
  );
}
