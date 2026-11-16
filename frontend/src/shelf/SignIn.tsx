/** Hosted mode only: the welcome a reader sees before signing in, and again if they are signed
 * out while the app is open (a request answered 401). One button hands them to the sign-in
 * service, which offers its own ways in; Mothli never sees a password. After signing in they come
 * back to the page they were on (`return_to`, a path inside this app only). Local mode has no
 * sign-in: the gate there renders the app untouched. */
import { type ReactNode, useEffect, useState } from "react";
import { SIGNED_OUT_EVENT, type OfflineSession } from "../pwa/offline";
import { Moth } from "../ui/Moth";
import { MothliMark } from "../ui/MothliMark";
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
            <div className="ml-spread-foot">
              {retried && !offline && (
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
                onClick={noteAttempt}
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

/** Hosted mode: the welcome while nobody is signed in, and as soon as any request says the
 * sign-in has ended. Local mode (`session` undefined): the app, always, and nothing listens. */
export function SignInGate({ session, children }: { session?: OfflineSession | null; children: ReactNode }) {
  const hosted = session !== undefined;
  const [ended, setEnded] = useState(false);
  useEffect(() => {
    if (!hosted) return;
    const signedOut = () => setEnded(true);
    window.addEventListener(SIGNED_OUT_EVENT, signedOut);
    return () => window.removeEventListener(SIGNED_OUT_EVENT, signedOut);
  }, [hosted]);
  useEffect(() => {
    if (session) forgetAttempt();
  }, [session]);
  if (!hosted) return <>{children}</>;
  if (session === null) return <SignIn />;
  if (ended) return <SignIn ended />;
  return <>{children}</>;
}
