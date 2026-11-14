import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Book, type ClassicsCatalog } from "../api";
import { readingStreak, recentDays } from "../journey/journey";
import { InstallApp } from "../pwa/InstallApp";
import {
  activeOfflineOwner,
  cacheBookForOffline,
  isBookSavedOffline,
  type OfflineSession,
} from "../pwa/offline";
import { AiStatus } from "../ui/AiStatus";
import { Button } from "../ui/Button";
import { Icon } from "../ui/Icon";
import { Moth } from "../ui/Moth";
import { MothliMark } from "../ui/MothliMark";
import { BOOK_ACCEPT, BOOK_FORMATS, bookFileKind, prepareBookUpload, unsupportedBookMessage } from "../import/bookFiles";
import { readLastPassage, sinceLabel } from "../reader/lastPassage";
import { ProviderSettings } from "./ProviderSettings";
import "./shelf-redesign.css";

const recentKey = (owner: string) => `litlet.library-recent:${owner}`;
function readRecent(owner: string): string[] {
  try {
    const value: unknown = JSON.parse(
      localStorage.getItem(recentKey(owner)) ?? "[]",
    );
    return Array.isArray(value)
      ? value.filter((id): id is string => typeof id === "string").slice(0, 20)
      : [];
  } catch {
    return [];
  }
}

/* Cloth bindings. Each book keeps the same cloth between visits. */
const coverTones = ["oxblood", "forest", "navy", "ochre", "plum", "slate"];
function toneOf(book: Book) {
  return coverTones[
    Array.from(book.title).reduce((sum, char) => sum + char.charCodeAt(0), 0) %
      coverTones.length
  ];
}

/** A cloth-bound cover: the title between two gilt rules, the author's surname at the foot. */
export function BookCover({
  book,
  className = "",
  art,
}: {
  book: Book;
  className?: string;
  /** Where the book's own cover picture is served, when it may have one. */
  art?: string;
}) {
  // The cloth cover is always drawn: it shows while the picture loads, and stays when there is none.
  const [picture, setPicture] = useState<"loading" | "shown" | "none">("loading");
  return (
    <span
      className={`book-cover cover-${toneOf(book)} ${className}`}
      data-art={art ? picture : undefined}
      aria-hidden="true"
    >
      <span className="cover-title">{book.title}</span>
      <span className="cover-author">
        {(book.author || "").split(/\s+/).pop() || "Your collection"}
      </span>
      {art && picture !== "none" && (
        <img
          className="cover-art"
          src={art}
          alt=""
          loading="lazy"
          decoding="async"
          onLoad={() => setPicture("shown")}
          onError={() => setPicture("none")}
        />
      )}
    </span>
  );
}

function StreakStrip() {
  const [days] = useState(() => recentDays());
  const [streak] = useState(() => readingStreak());
  const readCount = days.filter((day) => day.read).length;
  return (
    <section className="ml-streak" aria-labelledby="ml-streak-title">
      <ol className="ml-streak-days" aria-hidden="true">
        {days.map((day) => (
          <li
            key={day.key}
            className={`${day.read ? "is-read" : ""} ${day.today ? "is-today" : ""}`}
          >
            <span className="ml-streak-dot" />
            <span className="ml-streak-label">{day.label}</span>
          </li>
        ))}
      </ol>
      <p className="ml-streak-line">
        <strong id="ml-streak-title">
          {streak > 0 ? `${streak}-day streak` : "No streak yet"}
        </strong>
        <span>
          {readCount
            ? `You read on ${readCount} of the last 7 days.`
            : "Read a few pages today and I’ll start counting."}
        </span>
      </p>
    </section>
  );
}

type Journey = { id: string; done: number; total: number };

export function Shelf({
  onOpen,
  onBrowse,
  session,
}: {
  onOpen: (id: string) => void;
  /** Opens the free library; the tile is not shown without it. */
  onBrowse?: () => void;
  session?: OfflineSession | null;
}) {
  const owner = activeOfflineOwner();
  const [books, setBooks] = useState<Book[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [settingsAvailable, setSettingsAvailable] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [showUtilities, setShowUtilities] = useState(false);
  const [showGuide, setShowGuide] = useState(false);
  const [dark, setDark] = useState(() => document.documentElement.dataset.theme === "dark");
  const toggleTheme = () => {
    const next = !dark;
    setDark(next);
    document.documentElement.dataset.theme = next ? "dark" : "light";
    try {
      localStorage.setItem("mothli.theme", next ? "dark" : "light");
    } catch {
      /* optional */
    }
  };
  const [offlineBusy, setOfflineBusy] = useState<string | null>(null);
  const [offlineState, setOfflineState] = useState(() => ({
    owner,
    ids: new Set<string>(),
  }));
  const offlineIds =
    offlineState.owner === owner ? offlineState.ids : new Set<string>();
  const [offlineStatus, setOfflineStatus] = useState("");
  const [signingOut, setSigningOut] = useState(false);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<"all" | "recent" | "offline">("all");
  const [sort, setSort] = useState("added");
  const [layout, setLayout] = useState<"grid" | "list">("grid");
  const [dragging, setDragging] = useState(false);
  const [journey, setJourney] = useState<Journey | null>(null);
  // The free library: offered only when the server has a catalogue to offer.
  const [classics, setClassics] = useState<ClassicsCatalog | null>(null);
  const [recentState, setRecent] = useState(() => ({
    owner,
    ids: readRecent(owner),
  }));
  const recent =
    recentState.owner === owner ? recentState.ids : readRecent(owner);
  const fileInput = useRef<HTMLInputElement>(null);
  const utilities = useRef<HTMLDivElement>(null);
  const utilitiesToggle = useRef<HTMLButtonElement>(null);
  const dragDepth = useRef(0);
  const importLock = useRef(false);
  const mounted = useRef(true);
  const msg = (error: unknown) =>
    error instanceof Error ? error.message : String(error);
  const refresh = useCallback(async () => {
    setErr(null);
    try {
      const result = await api.books();
      if (mounted.current) setBooks(result);
    } catch (error) {
      if (mounted.current) setErr(msg(error));
    }
  }, []);
  const loadClassics = useCallback(async () => {
    try {
      const catalog = await Promise.resolve().then(() => api.classics({ limit: 1 }));
      if (mounted.current && catalog?.enabled && catalog.books.length) setClassics(catalog);
    } catch {
      /* no catalogue on this server: the shelf simply doesn't offer one */
    }
  }, []);
  useEffect(() => {
    mounted.current = true;
    void refresh();
    void loadClassics();
    api
      .providerSettings()
      .then(() => {
        if (mounted.current) setSettingsAvailable(true);
      })
      .catch(() => {});
    return () => {
      mounted.current = false;
    };
  }, [refresh, loadClassics]);
  useEffect(() => {
    let dead = false;
    if (!books || !("caches" in globalThis)) return;
    void Promise.all(
      books.map(async (book) =>
        (await isBookSavedOffline(book.book_id, owner)) ? book.book_id : null,
      ),
    ).then((saved) => {
      if (!dead)
        setOfflineState({
          owner,
          ids: new Set(saved.filter((id): id is string => id !== null)),
        });
    });
    return () => {
      dead = true;
    };
  }, [books, owner]);
  useEffect(() => {
    if (!showUtilities) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setShowUtilities(false);
        utilitiesToggle.current?.focus();
      }
    };
    const closeOutside = (event: PointerEvent) => {
      if (event.target instanceof Node && !utilities.current?.contains(event.target) && !utilitiesToggle.current?.contains(event.target)) {
        setShowUtilities(false);
      }
    };
    document.addEventListener("keydown", closeOnEscape);
    document.addEventListener("pointerdown", closeOutside);
    return () => {
      document.removeEventListener("keydown", closeOnEscape);
      document.removeEventListener("pointerdown", closeOutside);
    };
  }, [showUtilities]);
  useEffect(() => {
    if (showGuide) document.getElementById("companion-guide")?.scrollIntoView?.({ block: "nearest" });
  }, [showGuide]);

  const featured = books?.find((book) => book.book_id === recent[0]);
  const spotlight = featured ?? books?.[0];
  const spotlightId = spotlight?.book_id;
  /* Chapter path for the continue card: fetched lazily, and silently absent when unavailable. */
  useEffect(() => {
    if (!spotlightId) return;
    let dead = false;
    void Promise.all([
      Promise.resolve().then(() => api.position(spotlightId)),
      Promise.resolve()
        .then(() => api.manifest(spotlightId))
        .catch(() => null),
    ])
      .then(([position, manifest]) => {
        const total = manifest?.atoms?.length || position.atoms || 0;
        if (dead || !total) return;
        const done = Math.min(total, Math.max(0, Math.floor(Number(position.bookmark) || 0)));
        setJourney({ id: spotlightId, done, total });
      })
      .catch(() => {});
    return () => {
      dead = true;
    };
  }, [spotlightId]);

  const onFile = async (file: File | undefined) => {
    if (!file || importLock.current) return;
    const unsupported = unsupportedBookMessage(bookFileKind(file));
    if (unsupported) {
      setErr(unsupported);
      return;
    }
    importLock.current = true;
    setBusy(true);
    setErr(null);
    try {
      // The server only takes EPUB, so other formats are converted here first (see import/).
      await api.importBook(await prepareBookUpload(file));
      await refresh();
    } catch (error) {
      if (mounted.current) setErr(msg(error));
    } finally {
      importLock.current = false;
      if (mounted.current) setBusy(false);
    }
  };
  const saveOffline = async (book: Book) => {
    if (offlineBusy) return;
    setOfflineBusy(book.book_id);
    setOfflineStatus(`Saving ${book.title} for offline reading…`);
    try {
      await cacheBookForOffline(book.book_id);
      setOfflineState((current) => ({
        owner,
        ids: new Set([
          ...(current.owner === owner ? current.ids : []),
          book.book_id,
        ]),
      }));
      setOfflineStatus(`${book.title} is ready for offline reading.`);
    } catch (error) {
      setOfflineStatus(msg(error));
    } finally {
      setOfflineBusy(null);
    }
  };
  const openBook = (id: string) => {
    const next = [id, ...recent.filter((value) => value !== id)].slice(0, 20);
    setRecent({ owner, ids: next });
    try {
      localStorage.setItem(recentKey(owner), JSON.stringify(next));
    } catch {
      /* Optional convenience state. */
    }
    onOpen(id);
  };
  const logout = async () => {
    setSigningOut(true);
    setErr(null);
    try {
      await api.logout();
      location.assign("/api/auth/login?return_to=/");
    } catch (error) {
      setErr(msg(error));
      setSigningOut(false);
    }
  };
  const filtered = (books ?? [])
    .filter(
      (book) =>
        `${book.title} ${book.author ?? ""}`
          .toLocaleLowerCase()
          .includes(query.trim().toLocaleLowerCase()) &&
        (filter !== "offline" || offlineIds.has(book.book_id)) &&
        (filter !== "recent" || recent.includes(book.book_id)),
    )
    .sort((a, b) =>
      sort === "title"
        ? a.title.localeCompare(b.title)
        : sort === "author"
          ? (a.author ?? "").localeCompare(b.author ?? "")
          : filter === "recent"
            ? recent.indexOf(a.book_id) - recent.indexOf(b.book_id)
            : 0,
    );
  const browsingAll = filter === "all" && !query;
  const libraryEmpty = books !== null && books.length === 0;
  const spotJourney = journey && journey.id === spotlightId ? journey : null;
  const resuming = !!featured || (spotJourney?.done ?? 0) > 0;
  const addLabel = busy ? "adding a book" : "add a book";

  const offlineButton = (book: Book) => (
    <button
      className={`ml-offline ${offlineIds.has(book.book_id) ? "is-saved" : ""}`}
      disabled={!!offlineBusy}
      aria-label={
        offlineIds.has(book.book_id)
          ? "refresh offline copy"
          : "save for offline reading"
      }
      aria-describedby={`book-${book.book_id}-title`}
      title={offlineIds.has(book.book_id) ? "Saved offline. Refresh the copy" : "Save offline"}
      onClick={() => {
        void saveOffline(book);
      }}
    >
      <Icon
        name={offlineIds.has(book.book_id) ? "check" : "download"}
        size={14}
      />
      <span className="ml-offline-text">
        {offlineBusy === book.book_id
          ? "Saving…"
          : offlineIds.has(book.book_id)
            ? "Saved offline"
            : "Save offline"}
      </span>
    </button>
  );
  const bookName = (book: Book) =>
    book.author ? `${book.title}, ${book.author}` : book.title;

  // The lines the reader was last on, saved on this device while they read. No server, no model.
  const stopped = spotlight && resuming ? readLastPassage(owner, spotlight.book_id) : null;
  const progressLine = spotJourney
    ? spotJourney.done >= spotJourney.total
      ? `Finished, all ${spotJourney.total} chapters`
      : spotJourney.done === 0
        ? `Ready to begin, ${spotJourney.total} chapters ahead`
        : `Chapter ${spotJourney.done + 1} of ${spotJourney.total}`
    : null;

  return (
    <div
      className={`mothli-library ${dragging ? "is-dragging" : ""}`}
      onDragEnter={(event) => {
        if (!event.dataTransfer.types.includes("Files")) return;
        event.preventDefault();
        dragDepth.current++;
        setDragging(true);
      }}
      onDragOver={(event) => {
        if (event.dataTransfer.types.includes("Files")) event.preventDefault();
      }}
      onDragLeave={(event) => {
        event.preventDefault();
        if (--dragDepth.current <= 0) {
          dragDepth.current = 0;
          setDragging(false);
        }
      }}
      onDrop={(event) => {
        event.preventDefault();
        dragDepth.current = 0;
        setDragging(false);
        void onFile(event.dataTransfer.files[0]);
      }}
    >
      <a className="skip-link" href="#library-main">
        Skip to your library
      </a>

      <header className="ml-top">
        <a className="ml-brand" href="/" aria-label="Mothli home">
          <MothliMark className="ml-brand-mark" size={38} />
          <span>Mothli</span>
        </a>
        <h1 className="sr-only">Your library</h1>
        <div className="ml-top-tools">
          <button
            className="ml-guide-toggle"
            aria-expanded={showGuide}
            aria-controls="companion-guide"
            onClick={() => setShowGuide((value) => !value)}
          >
            Meet Mothli
          </button>
          <button
            className="ml-theme-toggle"
            aria-label={dark ? "Switch to light mode" : "Switch to dark mode"}
            title={dark ? "Day paper" : "Night paper"}
            onClick={toggleTheme}
          >
            {dark ? (
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true">
                <circle cx="12" cy="12" r="4" />
                <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
              </svg>
            ) : (
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5Z" />
              </svg>
            )}
          </button>
          <button
            ref={utilitiesToggle}
            className="ml-settings-toggle"
            aria-label="Library settings and account"
            aria-expanded={showUtilities}
            aria-controls="library-utilities"
            onClick={() => setShowUtilities((current) => !current)}
          >
            <Icon name={showUtilities ? "close" : "settings"} size={18} />
          </button>
        </div>
        <section
          className="ml-guide"
          id="companion-guide"
          hidden={!showGuide}
          aria-label="Meet Mothli"
        >
          <div className="ml-guide-head">
            <h2>Meet Mothli</h2>
            <button
              className="ml-guide-close"
              aria-label="Close companion introduction"
              onClick={() => {
                setShowGuide(false);
                document.querySelector<HTMLButtonElement>('[aria-controls="companion-guide"]')?.focus();
              }}
            >
              <Icon name="close" size={16} />
            </button>
          </div>
          <p className="m-hand">Hi, I’m Mothli. I’m an AI that reads along with you, and I only know the pages you’ve already read.</p>
          <ul>
            <li><strong>Read</strong> your book, at your pace.</li>
            <li><strong>Ask me</strong> “wait, who was that?”</li>
            <li><strong>Keep</strong> the good bits in your notebook.</li>
          </ul>
        </section>
        <div className="ml-utilities" ref={utilities} id="library-utilities" hidden={!showUtilities}>
          <strong className="ml-utilities-title">Your reading space</strong>
          {settingsAvailable && <Button variant="ghost" icon="settings" onClick={() => { utilitiesToggle.current?.focus(); setShowUtilities(false); setShowSettings(true); }}>Connection settings</Button>}
          <InstallApp />
          {session?.user && <span className="account-name">{session.user.display_name}</span>}
          {session === null && <a className="account-link" href="/api/auth/login?return_to=/">Sign in</a>}
          {session?.user && <Button variant="ghost" className="account-signout" icon="arrowRight" aria-label={signingOut ? "Signing out" : "Sign out and erase offline books"} title="Sign out and erase offline books" disabled={signingOut} onClick={() => { void logout(); }}>{signingOut ? "Signing out…" : "Sign out and erase offline books"}</Button>}
        </div>
      </header>

      <main id="library-main" className="ml-main" tabIndex={-1}>
        {err && (
          <div className="notice notice--error ml-notice" role="alert">
            <Icon name="info" />
            <span>{err}</span>
            {books === null && (
              <Button
                variant="ghost"
                icon="refresh"
                onClick={() => {
                  void refresh();
                }}
              >
                Try again
              </Button>
            )}
          </div>
        )}

        {/* The open book: the left page is the book you are in, the right page is Mothli's margin. */}
        {spotlight && browsingAll && (
          <section
            className="ml-spread"
            aria-label={featured ? "Recently opened book" : "Your next read"}
          >
            <div className="ml-page ml-page--left">
              <p className="m-kicker">{resuming ? "Continue reading" : "Your next read"}</p>
              <h2 className="ml-spread-title">{spotlight.title}</h2>
              <p className="ml-spread-author">{spotlight.author || "From your collection"}</p>
              <div className="ml-spread-foot">
                {spotJourney && progressLine && (
                  <div className="ml-progress">
                    <span className="ml-progress-bar" aria-hidden="true">
                      <span style={{ width: `${Math.round((spotJourney.done / spotJourney.total) * 100)}%` }} />
                    </span>
                    <p className="ml-progress-where">{progressLine}</p>
                  </div>
                )}
                <Button
                  variant="primary"
                  className="ml-spread-cta"
                  onClick={() => openBook(spotlight.book_id)}
                >
                  {resuming ? "Continue reading" : "Start reading"}
                </Button>
              </div>
            </div>
            <div className="ml-page ml-page--right">
              {stopped ? (
                <>
                  <p className="ml-stopped">{stopped.text}</p>
                  <div className="ml-note">
                    <Moth size={44} />
                    <div className="m-hand ml-note-text">
                      <p>You stopped here, {sinceLabel(stopped.at)}.</p>
                    </div>
                  </div>
                </>
              ) : (
                <div className="ml-note">
                  <Moth size={44} />
                  <div className="m-hand ml-note-text">
                    <p>{resuming ? "I remember where you stopped." : "This one is ready when you are."}</p>
                    <p>Ask me about a name or a scene and I’ll answer from the pages you’ve read.</p>
                  </div>
                </div>
              )}
              <StreakStrip />
            </div>
          </section>
        )}

        {libraryEmpty && browsingAll && (
          <section className="ml-spread" aria-label="Add your first book">
            <div className="ml-page ml-page--left">
              <p className="m-kicker">Chapter one</p>
              <h2 className="ml-spread-title">Make yourself at home.</h2>
              <p className="ml-spread-lede">This shelf is waiting for its first story. Add a book you own and I’ll follow along, one chapter at a time.</p>
              <div className="ml-spread-foot">
                <Button
                  variant="primary"
                  className="ml-spread-cta"
                  aria-label={addLabel}
                  aria-busy={busy}
                  disabled={busy}
                  onClick={() => fileInput.current?.click()}
                >
                  {busy ? "Adding your book…" : "Add a book"}
                </Button>
                <p className="ml-progress-where">or drop a book file anywhere</p>
                {classics && onBrowse && (
                  <button type="button" className="ml-classics-link" onClick={onBrowse}>
                    Nothing to hand? Browse the free library
                  </button>
                )}
              </div>
            </div>
            <div className="ml-page ml-page--right">
              <div className="ml-note ml-note--stacked">
                <Moth size={64} />
                <div className="m-hand ml-note-text ml-note-text--large">
                  <p>Hi, I’m Mothli. I’m an AI that reads along with you.</p>
                  <p>I only ever know the pages you’ve read, so I can’t spoil anything.</p>
                  <p>Ask me “wait, who was that?” whenever you like. I’ll write back in the margin.</p>
                </div>
              </div>
            </div>
          </section>
        )}

        <section className="ml-collection" aria-labelledby="collection-title">
          <div className="ml-collection-head">
            <h2 id="collection-title">
              {filter === "recent"
                ? "Recently opened"
                : filter === "offline"
                  ? "Available offline"
                  : "Your books"}
              <span className="ml-count">{books ? filtered.length : ""}</span>
            </h2>
            <nav className="ml-filters" aria-label="Your books">
              <button
                aria-label="All books"
                aria-current={filter === "all" ? "page" : undefined}
                onClick={() => setFilter("all")}
              >
                All
              </button>
              <button
                aria-label="Recently opened"
                aria-current={filter === "recent" ? "page" : undefined}
                onClick={() => setFilter("recent")}
              >
                Recent
              </button>
              <button
                aria-label="Available offline"
                aria-current={filter === "offline" ? "page" : undefined}
                onClick={() => setFilter("offline")}
              >
                Offline
              </button>
            </nav>
          </div>
          <div className="ml-toolbar">
            <label className="ml-search">
              <Icon name="search" size={16} />
              <span className="sr-only">Search your library</span>
              <input
                type="search"
                placeholder="Search books or authors"
                value={query}
                onChange={(event) => setQuery(event.currentTarget.value)}
              />
            </label>
            <label className="ml-sort">
              <span className="sr-only">Sort books</span>
              <select
                value={sort}
                onChange={(event) => setSort(event.currentTarget.value)}
              >
                <option value="added">Library order</option>
                <option value="title">Title A–Z</option>
                <option value="author">Author A–Z</option>
              </select>
              <Icon name="chevronDown" size={14} />
            </label>
            <div className="ml-view" role="group" aria-label="Library layout">
              <button
                aria-label="Grid view"
                title="Shelf"
                aria-pressed={layout === "grid"}
                onClick={() => setLayout("grid")}
              >
                <Icon name="library" size={17} />
              </button>
              <button
                aria-label="List view"
                title="List"
                aria-pressed={layout === "list"}
                onClick={() => setLayout("list")}
              >
                <Icon name="list" size={17} />
              </button>
            </div>
          </div>

          {books === null && !err ? (
            <div
              className="ml-shelves ml-shelves--loading"
              aria-label="Loading your library"
              aria-busy="true"
            >
              {[0, 1, 2, 3].map((item) => (
                <span className="ml-cover-ghost" key={item} />
              ))}
            </div>
          ) : books === null ? null : (
            <>
              {!filtered.length && !(browsingAll && libraryEmpty) && (
                <div className="ml-empty">
                  <Moth size={52} />
                  <div>
                    <h3>
                      {query
                        ? "No books found"
                        : filter === "offline"
                          ? "Take your stories with you."
                          : "Your next read is waiting."}
                    </h3>
                    <p>
                      {query
                        ? "Try a different title or author."
                        : filter === "offline"
                          ? "Choose Save offline on a book to read wherever you go."
                          : "Open a book and it will be easy to find here."}
                    </p>
                    {!browsingAll && (
                      <Button
                        onClick={() => {
                          setQuery("");
                          setFilter("all");
                        }}
                      >
                        Show all books
                      </Button>
                    )}
                  </div>
                </div>
              )}
              {filtered.length > 0 &&
                (layout === "grid" ? (
                  <div className="ml-shelves">
                    {filtered.map((book) => (
                      <article
                        className={`ml-book ${book.book_id === recent[0] ? "is-current" : ""}`}
                        key={book.book_id}
                      >
                        <button
                          className="ml-book-open"
                          aria-label={bookName(book)}
                          title={bookName(book)}
                          onClick={() => openBook(book.book_id)}
                        >
                          <BookCover book={book} art={`/api/books/${encodeURIComponent(book.book_id)}/cover`} />
                        </button>
                        <span className="sr-only" id={`book-${book.book_id}-title`}>{book.title}</span>
                        {offlineButton(book)}
                      </article>
                    ))}
                    {browsingAll && (
                      <div className="ml-book ml-book--add">
                        <button
                          className="ml-book-add"
                          aria-label={addLabel}
                          aria-busy={busy}
                          disabled={busy}
                          onClick={() => fileInput.current?.click()}
                        >
                          <Icon name="plus" size={24} />
                          <span className="ml-book-add-title">{busy ? "Adding…" : "Add a book"}</span>
                          <span className="ml-book-add-hint">Drop a book file here</span>
                        </button>
                      </div>
                    )}
                    {browsingAll && classics && onBrowse && (
                      <div className="ml-book ml-book--add">
                        <button className="ml-book-add ml-book-classics" onClick={onBrowse}>
                          <Moth size={44} />
                          <span className="ml-book-add-title">Browse the library</span>
                          <span className="ml-book-add-hint">{classics.total.toLocaleString("en")} free books to choose from</span>
                        </button>
                      </div>
                    )}
                  </div>
                ) : (
                  <ul className="ml-list">
                    {filtered.map((book) => (
                      <li key={book.book_id}>
                        <article className="ml-row">
                          <button
                            className="ml-row-open"
                            aria-label={bookName(book)}
                            onClick={() => openBook(book.book_id)}
                          >
                            <span className={`ml-row-spine cover-${toneOf(book)}`} aria-hidden="true" />
                            <span className="ml-row-text">
                              <span className="ml-row-title" id={`book-${book.book_id}-title`}>{book.title}</span>
                              <span className="ml-row-author">{book.author || "Unknown author"}</span>
                            </span>
                            <span className={`ml-row-state ${recent.includes(book.book_id) ? "is-recent" : ""}`}>
                              {recent.includes(book.book_id) ? "Recently opened" : "Ready to read"}
                            </span>
                          </button>
                          {offlineButton(book)}
                        </article>
                      </li>
                    ))}
                    {browsingAll && (
                      <li>
                        <button
                          className="ml-row-add"
                          aria-label={addLabel}
                          aria-busy={busy}
                          disabled={busy}
                          onClick={() => fileInput.current?.click()}
                        >
                          <Icon name="plus" size={16} />
                          {busy ? "Adding your book…" : "Add a book"}
                        </button>
                      </li>
                    )}
                    {browsingAll && classics && onBrowse && (
                      <li>
                        <button className="ml-row-add" onClick={onBrowse}>
                          <Icon name="book" size={16} />
                          Browse the library
                        </button>
                      </li>
                    )}
                  </ul>
                ))}
            </>
          )}
        </section>

        <footer className="ml-footer">
          <p className="ml-footer-line">Read a little. Remember a lot.</p>
          <div className="ml-footer-meta">
            <span className="ml-safe"><Icon name="shield" size={14} /> Spoiler safe</span>
            <span>Your own books, kept in your own collection</span>
          </div>
          <AiStatus compact />
        </footer>
      </main>

      {showSettings && (
        <ProviderSettings onClose={() => setShowSettings(false)} />
      )}
      {offlineStatus && (
        <div className="library-toast ml-toast" aria-live="polite">
          <Icon name="download" size={17} />
          {offlineStatus}
          <button
            className="plain"
            aria-label="Dismiss offline message"
            onClick={() => setOfflineStatus("")}
          >
            <Icon name="close" size={17} />
          </button>
        </div>
      )}
      <div role="status" className="sr-only">
        {busy ? "adding a book…" : ""}
      </div>
      {dragging && (
        <div className="import-drop-overlay ml-drop" aria-hidden="true">
          <Moth size={72} />
          <h2>Drop your next read here.</h2>
          <p>{BOOK_FORMATS}, without copy protection</p>
        </div>
      )}
      <input
        ref={fileInput}
        type="file"
        accept={BOOK_ACCEPT}
        aria-label="import a book file"
        hidden
        onChange={(event) => {
          const file = event.currentTarget.files?.[0];
          event.currentTarget.value = "";
          void onFile(file);
        }}
      />
    </div>
  );
}
