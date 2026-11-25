import "@fontsource/eb-garamond/400.css";
import "@fontsource/eb-garamond/400-italic.css";
import "@fontsource/eb-garamond/500.css";
import "@fontsource-variable/inter/index.css";
import "@fontsource-variable/cormorant-garamond/index.css";
import "@fontsource-variable/cormorant-garamond/wght-italic.css";
import "@fontsource-variable/caveat/index.css";
import "@fontsource-variable/literata/index.css";
import "@fontsource-variable/literata/wght-italic.css";
import "./styles.css";

import React, { useEffect, useState } from "react";
import ReactDOM from "react-dom/client";
import { initializeOfflineSupport, offersChat, type OfflineSession } from "./pwa/offline";
import { ReadingHistory } from "./history/ReadingHistory";
import { FreeLibrary } from "./library/FreeLibrary";
import { routedPublicPage } from "./public/pages";
import { PublicPage, preloadSite } from "./public/PublicPage";
import { ReaderPage } from "./reader/ReaderPage";
import { Shelf } from "./shelf/Shelf";
import { SignInGate } from "./shelf/SignIn";
import { Icon } from "./ui/Icon";
import { Button } from "./ui/Button";
import { AiRuntimeProvider } from "./ui/AiStatus";
import { setMothliStanding } from "./ui/mothliStanding";
import "./experience.css";
import "./theme/mothli.css";
import "./theme/bookish.css";

// Light or dark, remembered per device; the reader keeps it in sync with its theme preference.
try {
  const savedTheme = localStorage.getItem("mothli.theme");
  document.documentElement.dataset.theme = savedTheme === "dark" ? "dark" : "light";
} catch {
  document.documentElement.dataset.theme = "light";
}

// Router-less view switch for the shelf, the free library and the reader.
const currentBookId = () =>
  new URLSearchParams(window.location.search).get("book");
const inLibrary = () => new URLSearchParams(window.location.search).has("library");
const inHistory = () => new URLSearchParams(window.location.search).has("history");

function App({ session }: { session?: OfflineSession | null }) {
  const [bookId, setBookId] = useState<string | null>(currentBookId);
  const [browsing, setBrowsing] = useState(inLibrary);
  const [recalling, setRecalling] = useState(inHistory);
  useEffect(() => {
    const restoreLocation = () => {
      setBookId(currentBookId());
      setBrowsing(inLibrary());
      setRecalling(inHistory());
    };
    window.addEventListener("popstate", restoreLocation);
    return () => window.removeEventListener("popstate", restoreLocation);
  }, []);
  const open = (id: string) => {
    history.pushState(null, "", `/?book=${encodeURIComponent(id)}`);
    setBookId(id);
  };
  const back = () => {
    history.pushState(null, "", "/");
    setBookId(null);
    setBrowsing(false);
    setRecalling(false);
  };
  const recall = () => {
    history.pushState(null, "", "/?history=1");
    setRecalling(true);
    window.scrollTo(0, 0);
  };
  const browse = () => {
    history.pushState(null, "", "/?library=1");
    setBrowsing(true);
    window.scrollTo(0, 0);
  };
  // Hosted mode (a session, even a signed-out one) has no reading history from other apps yet.
  const withHistory = session === undefined;
  // key: a book->book transition must fully unmount the old reader (its view/reporter must never
  // route a stale offset into ANOTHER book's permanent bookmark ratchet — pass-2 F4)
  return bookId ? (
    <ReaderPage key={bookId} bookId={bookId} onBack={back} hosted={session !== undefined}
      chat={offersChat(session)} />
  ) : recalling && withHistory ? (
    <ReadingHistory onBack={back} onOpen={open} />
  ) : browsing ? (
    <FreeLibrary onBack={back} onOpen={open} />
  ) : (
    <Shelf onOpen={open} onBrowse={browse} onHistory={recall} session={session} />
  );
}

type Initialization =
  | { ready: false; failed: boolean }
  | { ready: true; session?: OfflineSession | null };

async function initialize(): Promise<OfflineSession | null | undefined> {
  const session = await initializeOfflineSupport();
  // Where the reader stands with Mothli; every chat, the cast and "Story so far" follow it.
  setMothliStanding(session?.mothli ?? null, session?.user?.email ?? null, session?.user?.id ?? null);
  return session;
}

function Boot({ initial }: { initial?: Initialization }) {
  const [initialization, setInitialization] = useState<Initialization>(
    initial ?? { ready: false, failed: false },
  );
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    // Already done before the first draw (a page the server wrote out, below).
    if (initial && attempt === 0) return;
    let active = true;
    setInitialization({ ready: false, failed: false });
    void initialize()
      .then((session) => {
        if (!active) return;
        setInitialization({ ready: true, session });
      })
      .catch(() => {
        if (active) setInitialization({ ready: false, failed: true });
      });
    return () => {
      active = false;
    };
  }, [attempt, initial]);

  // Session resolution establishes the offline owner. Keep book requests behind that boundary,
  // while painting an account-neutral shell immediately on slow connections.
  if (!initialization.ready) {
    return (
      <main className="app-boot" aria-busy={!initialization.failed}>
        <Icon name="book" size={34} />
        <header>
          <p className="smallcaps">Mothli</p>
          <h1>Your next chapter.</h1>
        </header>
        <p className="quiet" role={initialization.failed ? "alert" : "status"}>
          {initialization.failed
            ? "Your reading space could not be opened."
            : "Opening your reading space…"}
        </p>
        {initialization.failed && (
          <Button
            variant="primary"
            onClick={() => setAttempt((value) => value + 1)}
          >
            Try again
          </Button>
        )}
      </main>
    );
  }
  // Hosted: the public pages (ADR 0045 step 8) at their own addresses, signed in or not.
  const page = routedPublicPage(initialization.session, window.location.pathname);
  if (page) {
    return (
      <React.StrictMode>
        <PublicPage page={page} />
      </React.StrictMode>
    );
  }
  return (
    <React.StrictMode>
      <AiRuntimeProvider enabled={initialization.session === undefined}>
        {/* Hosted: the welcome until someone is signed in, and again if a request says they no
            longer are. Local: the app as it is. */}
        <SignInGate session={initialization.session}>
          <App session={initialization.session} />
        </SignInGate>
      </AiRuntimeProvider>
    </React.StrictMode>
  );
}

const container = document.getElementById("root")!;
const root = ReactDOM.createRoot(container);
if (container.firstElementChild) {
  // A public page the server wrote out to be read without JavaScript (ADR 0045 step 8;
  // public/prerender.tsx). It stays on screen until the app can draw the same page, prices and
  // all, and the app's first draw then replaces it with the same thing: nothing flashes.
  void Promise.all([initialize(), preloadSite(2_500)]).then(
    ([session]) => root.render(<Boot initial={{ ready: true, session }} />),
    () => root.render(<Boot initial={{ ready: false, failed: true }} />),
  );
} else {
  root.render(<Boot />);
}
