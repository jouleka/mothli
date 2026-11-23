/** Hosted mode only: the welcome a reader sees before signing in, and again if they are signed
 * out while the app is open (a request answered 401). One button hands them to the sign-in
 * service, which offers its own ways in; Mothli never sees a password. After signing in they come
 * back to the page they were on (`return_to`, a path inside this app only). Local mode has no
 * sign-in: the gate there renders the app untouched. */
import { type ReactNode, useEffect, useRef, useState } from "react";
import { api } from "../api";
import { PENDING_DELETION_EVENT, SIGNED_OUT_EVENT, type OfflineSession } from "../pwa/offline";
import { Button } from "../ui/Button";
import { Moth } from "../ui/Moth";
import { SIGN_UP_LINE } from "../ui/MothliPlan";
import { MothliMark } from "../ui/MothliMark";
import { TakeACopy } from "./TakeACopy";
import "./shelf-redesign.css";
import "./account.css";

/** When the reader last left for the sign-in service, so a return that is still signed out says
 * so once instead of looking as if nothing happened. Never sent anywhere. */
const ATTEMPT_KEY = "mothli.sign-in-attempt";
const ATTEMPT_WINDOW_MS = 10 * 60_000;

function recentAttempt(): boolean {
  try {
    const at = Number(sessionStorage.getItem(ATTEMPT_KEY));
    return Number.isFinite(at) && at > 0 && Date.now() - at < ATTEMPT_WINDOW_MS;
  } catch {
    return false;
  }
}

function noteAttempt(): void {
  try {
    sessionStorage.setItem(ATTEMPT_KEY, String(Date.now()));
  } catch {
    /* optional */
  }
}

function forgetAttempt(): void {
  try {
    sessionStorage.removeItem(ATTEMPT_KEY);
  } catch {
    /* optional */
  }
}

/** Set when the reader has just asked to delete their account, so the welcome they land on
 * says it is done and names the day. Kept on this tab only; gone once they sign in again. */
const DELETED_KEY = "mothli.account-deleted";

export function noteAccountDeleted(eraseAfter: string): void {
  try {
    sessionStorage.setItem(DELETED_KEY, eraseAfter);
  } catch {
    /* optional */
  }
}

function deletedNote(): string | null {
  try {
    return sessionStorage.getItem(DELETED_KEY);
  } catch {
    return null;
  }
}

function forgetDeleted(): void {
  try {
    sessionStorage.removeItem(DELETED_KEY);
  } catch {
    /* optional */
  }
}

/** When an account will be erased, as a person would say it: "on Thursday 17 December" (with the
 * year when it is not this year). The server's moment is the start of a UTC day and the daily
 * erasure runs after it; the day named is the one before, so wherever the reader lives the
 * erasure never comes before the day they were told. */
export function erasureDay(eraseAfter: string, now = new Date()): string {
  const at = new Date(eraseAfter);
  if (Number.isNaN(at.getTime())) return "in 30 days";
  const day = new Date(at.getTime() - 1);
  const part = (options: Intl.DateTimeFormatOptions) =>
    day.toLocaleDateString("en-GB", { ...options, timeZone: "UTC" });
  const sameYear = day.getUTCFullYear() === now.getUTCFullYear();
  // Put together by hand: browsers disagree on the comma after the weekday.
  const said = `${part({ weekday: "long" })} ${day.getUTCDate()} ${part({ month: "long" })}`;
  return `on ${said}${sameYear ? "" : ` ${day.getUTCFullYear()}`}`;
}

/** The sign-in service's door, asked to bring the reader back to `path` (this app's own path and
 * query only, as the server also insists; anything else comes back to the shelf). */
export function signInHref(path = `${window.location.pathname}${window.location.search}`): string {
  const safe = path.startsWith("/") && !path.startsWith("//") && !path.includes("\\") && path.length <= 1024
    ? path
    : "/";
  return `/api/auth/login?return_to=${encodeURIComponent(safe)}`;
}

export function SignIn({ ended = false }: { ended?: boolean }) {
  const [retried] = useState(recentAttempt);
  const [deleted] = useState(deletedNote);
  const [offline, setOffline] = useState(() => typeof navigator !== "undefined" && !navigator.onLine);
  useEffect(() => {
    const update = () => setOffline(!navigator.onLine);
    window.addEventListener("online", update);
    window.addEventListener("offline", update);
    return () => {
      window.removeEventListener("online", update);
      window.removeEventListener("offline", update);
    };
  }, []);

  return (
    <div className="mothli-library ml-welcome">
      <header className="ml-top">
        <span className="ml-brand">
          <MothliMark className="ml-brand-mark" size={38} />
          <span>Mothli</span>
        </span>
      </header>
      <main className="ml-main">
        <section className="ml-spread" aria-labelledby="welcome-title">
          <div className="ml-page ml-page--left">
            <p className="m-kicker">{ended ? "Welcome back" : "Welcome"}</p>
            <h1 id="welcome-title" className="ml-spread-title">
              {ended ? "Let’s get you back to your page." : "Read with a friend in the margin."}
            </h1>
            <p className="ml-spread-lede">
              {ended
                ? "You’ve been signed out. Sign in again and I’ll take you straight back to where you were."
                : "Bring a book you love. Mothli reads along with you and only knows it as far as you’ve read, so it can never spoil what happens next."}
            </p>
            {!ended && <p className="ml-spread-lede">{SIGN_UP_LINE}</p>}
            <div className="ml-spread-foot">
              {deleted && (
                <p className="ml-welcome-note" role="status">
                  Done. You’re signed out everywhere, and your account will be erased{" "}
                  {erasureDay(deleted)} unless you sign in again before then.
                </p>
              )}
              {retried && !offline && !deleted && (
                <p className="ml-welcome-note" role="status">
                  Hmm, that didn’t quite work. Let’s try once more.
                </p>
              )}
              {offline && (
                <p className="ml-welcome-note" role="status">
                  You’re offline right now. Sign in once you’re back on the internet.
                </p>
              )}
              <a
                className="ui-button ui-button--primary ml-spread-cta ml-welcome-cta"
                href={signInHref()}
                aria-describedby="welcome-door"
                onClick={() => {
                  noteAttempt();
                  forgetDeleted();
                }}
              >
                {ended ? "Sign in again" : "Sign in to start reading"}
              </a>
              <p id="welcome-door" className="ml-progress-where">
                You’ll sign in on a secure page of its own. Mothli never sees your password.
              </p>
            </div>
          </div>
          <div className="ml-page ml-page--right">
            <div className="ml-note ml-note--stacked">
              <Moth size={64} />
              <div className="m-hand ml-note-text ml-note-text--large">
                <p>Hi, I’m Mothli.</p>
                <p>Lost track of who someone is? Just ask me.</p>
                <p>I only know the pages you’ve read, so no spoilers. Promise.</p>
              </div>
            </div>
          </div>
        </section>
      </main>
    </div>
  );
}

const KEEP_FAILED = "That didn’t work just now. Check your connection and try again.";

/** Hosted mode: a reader who asked to delete their account and signed in again within the 30
 * days. Nothing else of the app is open to them (the server refuses it too) until they choose,
 * except taking a copy of their data first. */
export function PendingDeletion({ eraseAfter }: { eraseAfter: string }) {
  const [working, setWorking] = useState<"keep" | "leave" | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);
  useEffect(() => headingRef.current?.focus(), []);

  const keep = async () => {
    setWorking("keep");
    setProblem(null);
    try {
      await api.keepAccount();
      // The app again, from the start, with everything as it was.
      window.location.assign(`${window.location.pathname}${window.location.search}`);
    } catch {
      setProblem(KEEP_FAILED);
      setWorking(null);
    }
  };
  const leave = async () => {
    setWorking("leave");
    setProblem(null);
    try {
      await api.logout();
      window.location.assign("/");
    } catch {
      setProblem(KEEP_FAILED);
      setWorking(null);
    }
  };

  return (
    <div className="mothli-library ml-welcome">
      <header className="ml-top">
        <span className="ml-brand">
          <MothliMark className="ml-brand-mark" size={38} />
          <span>Mothli</span>
        </span>
      </header>
      <main className="ml-main">
        <section className="ml-spread" aria-labelledby="pending-title">
          <div className="ml-page ml-page--left">
            <p className="m-kicker">Your account</p>
            <h1 id="pending-title" className="ml-spread-title" ref={headingRef} tabIndex={-1}>
              Your account is set to be erased {erasureDay(eraseAfter)}.
            </h1>
            <p className="ml-spread-lede">
              You asked for it to be deleted. Until then, everything is still here: your books,
              your place in them, your conversations and what Mothli remembers. Keep your account
              and it all comes back, just as you left it.
            </p>
            <div className="ml-spread-foot">
              {problem && <p className="fl-error" role="alert">{problem}</p>}
              <div className="ml-pending-actions">
                <Button
                  variant="primary"
                  disabled={working !== null}
                  aria-busy={working === "keep" || undefined}
                  onClick={() => void keep()}
                >
                  {working === "keep" ? "Keeping it…" : "Keep my account"}
                </Button>
                <Button
                  variant="secondary"
                  disabled={working !== null}
                  aria-busy={working === "leave" || undefined}
                  onClick={() => void leave()}
                >
                  {working === "leave" ? "Signing out…" : "Sign out"}
                </Button>
              </div>
              <p className="ml-progress-where">
                If you sign out and don’t come back, it will all be erased for good on that day.
              </p>
              <TakeACopy what="Leaving anyway? Take everything that’s yours with you first, in one file. The books themselves aren’t in it." />
            </div>
          </div>
          <div className="ml-page ml-page--right">
            <div className="ml-note ml-note--stacked">
              <Moth size={64} />
              <div className="m-hand ml-note-text ml-note-text--large">
                <p>I’ve kept your shelf just as you left it.</p>
                <p>Stay or go, it’s your choice.</p>
              </div>
            </div>
          </div>
        </section>
      </main>
    </div>
  );
}

/** Hosted mode: the welcome while nobody is signed in, and as soon as any request says the
 * sign-in has ended; the choice to keep the account while it waits to be erased, from the
 * session or from any request that says so. Local mode (`session` undefined): the app, always,
 * and nothing listens. */
export function SignInGate({ session, children }: { session?: OfflineSession | null; children: ReactNode }) {
  const hosted = session !== undefined;
  const [ended, setEnded] = useState(false);
  const [pending, setPending] = useState<string | null>(null);
  useEffect(() => {
    if (!hosted) return;
    const signedOut = () => setEnded(true);
    const waiting = (event: Event) => {
      const detail = (event as CustomEvent<unknown>).detail;
      if (typeof detail === "string") setPending(detail);
    };
    window.addEventListener(SIGNED_OUT_EVENT, signedOut);
    window.addEventListener(PENDING_DELETION_EVENT, waiting);
    return () => {
      window.removeEventListener(SIGNED_OUT_EVENT, signedOut);
      window.removeEventListener(PENDING_DELETION_EVENT, waiting);
    };
  }, [hosted]);
  useEffect(() => {
    if (session) {
      forgetAttempt();
      forgetDeleted();
    }
  }, [session]);
  if (!hosted) return <>{children}</>;
  if (session === null) return <SignIn />;
  if (ended) return <SignIn ended />;
  const eraseAfter = session.erase_after ?? pending;
  if (eraseAfter) return <PendingDeletion eraseAfter={eraseAfter} />;
  return <>{children}</>;
}
