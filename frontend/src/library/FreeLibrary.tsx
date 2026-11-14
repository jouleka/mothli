/** The Library: a page for finding a public-domain book to read. A reader can search, pick a mood
 * or a genre, or just wander along the shelves; a book is only downloaded when they add it. The
 * catalogue lives on the reader's own server, which also serves the cover art. */
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode, type RefObject } from "react";
import {
  api,
  type Book,
  type Classic,
  type ClassicDetail,
  type ClassicShelf,
  type ClassicsCatalog,
  type ClassicsSearch,
} from "../api";
import { BookCover } from "../shelf/Shelf";
import { Button } from "../ui/Button";
import { Icon } from "../ui/Icon";
import { Moth } from "../ui/Moth";
import { MothliMark } from "../ui/MothliMark";
import "./free-library.css";

const SEARCH_DELAY_MS = 250;
const LANGUAGE_CHOICES = 40;
const GHOSTS = Array.from({ length: 12 }, (_, index) => index);

type Filters = { q: string; language: string; genre: string; mood: string };

const count = (n: number) => n.toLocaleString("en");

function languageName(code: string) {
  try {
    return new Intl.DisplayNames(["en"], { type: "language" }).of(code) ?? code;
  } catch {
    return code;
  }
}

function readingTime(minutes: number | null) {
  if (!minutes) return null;
  if (minutes < 50) return `About ${Math.max(5, Math.round(minutes / 5) * 5)} minutes`;
  const hours = Math.round(minutes / 30) / 2;
  return `About ${hours === 1 ? "an hour" : `${hours} hours`}`;
}

function filtersFromLocation(): Filters {
  const params = new URLSearchParams(window.location.search);
  return {
    q: params.get("q") ?? "",
    language: params.get("lang") ?? "",
    genre: params.get("kind") ?? "",
    mood: params.get("feel") ?? "",
  };
}

function rememberFilters(filters: Filters) {
  const params = new URLSearchParams();
  params.set("library", "1");
  if (filters.q.trim()) params.set("q", filters.q.trim());
  if (filters.language) params.set("lang", filters.language);
  if (filters.genre) params.set("kind", filters.genre);
  if (filters.mood) params.set("feel", filters.mood);
  history.replaceState(null, "", `/?${params.toString()}`);
}

/** Real cover art over a cloth cover: the cloth shows while the picture loads and when there is none. */
function Cover({ book }: { book: Classic }) {
  const [state, setState] = useState<"loading" | "shown" | "none">("loading");
  const cloth: Book = { book_id: book.id, title: book.title, author: book.author };
  return (
    <span className="fl-cover" data-art={state}>
      <BookCover book={cloth} />
      {state !== "none" && (
        <img
          src={`/api/classics/${encodeURIComponent(book.id)}/cover`}
          alt=""
          loading="lazy"
          decoding="async"
          draggable={false}
          onLoad={() => setState("shown")}
          onError={() => setState("none")}
        />
      )}
    </span>
  );
}

function BookCard({ book, onShelf, onPick }: { book: Classic; onShelf: boolean; onPick: (book: Classic) => void }) {
  return (
    <button type="button" className="fl-card" onClick={() => onPick(book)} aria-label={`${book.title}, ${book.author}`}>
      <Cover book={book} />
      <span className="fl-card-title">{book.title}</span>
      <span className="fl-card-author">{book.author}</span>
      {onShelf && (
        <span className="fl-card-owned">
          <Icon name="check" size={12} />
          On your shelf
        </span>
      )}
    </button>
  );
}

function Ghosts() {
  return (
    <>
      {GHOSTS.map((index) => (
        <li key={index} className="fl-ghost" aria-hidden="true">
          <span />
          <i />
          <i />
        </li>
      ))}
    </>
  );
}

const DRAG_STARTS_AFTER_PX = 6;

/** Lets a sideways row be moved the ways a hand expects: grab it with the mouse and pull, or (when
 * `wheel` is set) roll the mouse wheel over it. Touch and trackpads already scroll it natively.
 * A pull that moved the row is not also a click on whatever was under the pointer. */
function useSlide(ref: RefObject<HTMLElement | null>, { wheel = false }: { wheel?: boolean } = {}) {
  useEffect(() => {
    const row = ref.current;
    if (!row) return;
    let start: { x: number; left: number; id: number } | null = null;
    let pulled = false;

    const down = (event: PointerEvent) => {
      if (event.pointerType !== "mouse" || event.button !== 0) return;
      start = { x: event.clientX, left: row.scrollLeft, id: event.pointerId };
      pulled = false;
    };
    const move = (event: PointerEvent) => {
      if (!start || event.pointerId !== start.id) return;
      const moved = event.clientX - start.x;
      if (!pulled && Math.abs(moved) < DRAG_STARTS_AFTER_PX) return;
      if (!pulled) {
        pulled = true;
        row.dataset.pulling = "true";
        row.setPointerCapture?.(start.id);
      }
      row.scrollLeft = start.left - moved;
    };
    const up = (event: PointerEvent) => {
      if (!start || event.pointerId !== start.id) return;
      row.releasePointerCapture?.(start.id);
      delete row.dataset.pulling;
      start = null;
    };
    const click = (event: MouseEvent) => {
      if (!pulled) return;
      pulled = false;
      event.preventDefault();
      event.stopPropagation();
    };
    const roll = (event: WheelEvent) => {
      // a sideways gesture (trackpad, tilt wheel) scrolls the row by itself; turn an up/down roll
      // into a sideways one, unless the row is already at that end, so the page can still move on
      if (event.ctrlKey || Math.abs(event.deltaX) >= Math.abs(event.deltaY)) return;
      const room = event.deltaY > 0 ? row.scrollWidth - row.clientWidth - row.scrollLeft : row.scrollLeft;
      if (room <= 1) return;
      event.preventDefault();
      row.scrollLeft += event.deltaMode === 1 ? event.deltaY * 32 : event.deltaY;
    };

    row.addEventListener("pointerdown", down);
    row.addEventListener("pointermove", move);
    row.addEventListener("pointerup", up);
    row.addEventListener("pointercancel", up);
    row.addEventListener("click", click, true);
    if (wheel) row.addEventListener("wheel", roll, { passive: false });
    return () => {
      row.removeEventListener("pointerdown", down);
      row.removeEventListener("pointermove", move);
      row.removeEventListener("pointerup", up);
      row.removeEventListener("pointercancel", up);
      row.removeEventListener("click", click, true);
      row.removeEventListener("wheel", roll);
    };
  }, [ref, wheel]);
}

/** One line of chips that slides sideways: pull it, or roll the wheel over it. It fades at
 * whichever edge has more beyond it. */
function ChipRow({ labelledBy, heading, children }: { labelledBy: string; heading: ReactNode; children: ReactNode }) {
  const list = useRef<HTMLUListElement>(null);
  const [more, setMore] = useState({ left: false, right: false });
  useSlide(list, { wheel: true });
  const measure = useCallback(() => {
    const row = list.current;
    if (!row) return;
    const left = row.scrollLeft > 4;
    const right = row.scrollLeft + row.clientWidth < row.scrollWidth - 4;
    setMore((prev) => (prev.left === left && prev.right === right ? prev : { left, right }));
  }, []);
  useEffect(() => {
    measure();
    const row = list.current;
    if (!row || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(row);
    return () => observer.disconnect();
  }, [measure, children]);
  return (
    <div className="fl-chips" role="group" aria-labelledby={labelledBy}>
      {heading}
      <ul ref={list} className="fl-slide" onScroll={measure} data-more-left={more.left} data-more-right={more.right}>
        {children}
      </ul>
    </div>
  );
}

function ShelfRow({
  shelf,
  owned,
  onPick,
  onSeeAll,
}: {
  shelf: ClassicShelf;
  owned: (book: Classic) => boolean;
  onPick: (book: Classic) => void;
  onSeeAll?: () => void;
}) {
  const row = useRef<HTMLUListElement>(null);
  useSlide(row);
  const headingId = `fl-shelf-${shelf.kind}-${shelf.id}`;
  const slide = (direction: 1 | -1) => {
    const list = row.current;
    if (list) list.scrollBy({ left: direction * Math.max(list.clientWidth - 160, 200), behavior: "smooth" });
  };
  return (
    <section className="fl-shelf" aria-labelledby={headingId}>
      <header className="fl-shelf-head">
        <h2 id={headingId}>{shelf.title}</h2>
        <div className="fl-shelf-tools">
          {onSeeAll && (
            <button type="button" className="fl-link" onClick={onSeeAll} aria-label={`See all: ${shelf.title}`}>
              See all
            </button>
          )}
          <button type="button" className="fl-arrow" aria-label={`Earlier in ${shelf.title}`} onClick={() => slide(-1)}>
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="m15 5-7 7 7 7" />
            </svg>
          </button>
          <button type="button" className="fl-arrow" aria-label={`Later in ${shelf.title}`} onClick={() => slide(1)}>
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="m9 5 7 7-7 7" />
            </svg>
          </button>
        </div>
      </header>
      <ul className="fl-row fl-slide" ref={row}>
        {shelf.books.map((book) => (
          <li key={book.id}>
            <BookCard book={book} onShelf={owned(book)} onPick={onPick} />
          </li>
        ))}
      </ul>
    </section>
  );
}

function BookSheet({
  book,
  shelfId,
  onAdded,
  onOpen,
  onClose,
}: {
  book: Classic;
  shelfId: string | null;
  onAdded: (book: Classic, bookId: string) => void;
  onOpen: (bookId: string) => void;
  onClose: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [detail, setDetail] = useState<ClassicDetail | null>(null);
  const [adding, setAdding] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    ref.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    // the page behind stays put while a book is open
    const scroll = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = scroll;
    };
  }, [onClose]);

  useEffect(() => {
    let stale = false;
    api
      .classic(book.id)
      .then((found) => {
        if (!stale) setDetail(found);
      })
      .catch(() => {});
    return () => {
      stale = true;
    };
  }, [book.id]);

  const owned = shelfId ?? detail?.book_id ?? null;
  const add = async () => {
    setAdding(true);
    setError(null);
    try {
      const added = await api.addClassic(book.id);
      onAdded(book, added.book_id);
    } catch (problem) {
      setError(problem instanceof Error && problem.message ? problem.message : "Couldn’t add this book. Try again.");
    } finally {
      setAdding(false);
    }
  };

  const facts = [
    readingTime(detail?.minutes ?? null),
    book.language !== "en" ? languageName(book.language) : null,
    ...(detail?.genres ?? []).slice(0, 3).map((genre) => genre.label),
  ].filter(Boolean);

  return (
    <div className="fl-scrim" onPointerDown={(event) => event.target === event.currentTarget && onClose()}>
      <div ref={ref} className="fl-sheet" role="dialog" aria-modal="true" aria-labelledby="fl-sheet-title" tabIndex={-1}>
        <button className="fl-sheet-close" aria-label="Close" onClick={onClose}>
          <Icon name="close" size={18} />
        </button>
        <div className="fl-sheet-cover">
          <Cover book={book} />
        </div>
        <div className="fl-sheet-body">
          <h2 id="fl-sheet-title">{book.title}</h2>
          <p className="fl-sheet-author">{book.author}</p>
          {facts.length > 0 && <p className="fl-sheet-facts">{facts.join(" · ")}</p>}
          {detail?.blurb ? (
            <p className="fl-sheet-blurb">{detail.blurb}</p>
          ) : (
            detail && <p className="fl-sheet-blurb fl-sheet-blurb--none">No description for this one. Open it and see.</p>
          )}
          {error && (
            <p className="fl-error" role="alert">
              {error}
            </p>
          )}
          <div className="fl-sheet-actions">
            {owned ? (
              <>
                <Button variant="primary" onClick={() => onOpen(owned)}>
                  Start reading
                </Button>
                <span className="fl-sheet-owned">
                  <Icon name="check" size={14} />
                  On your shelf
                </span>
              </>
            ) : (
              <Button variant="primary" aria-busy={adding} disabled={adding} onClick={() => void add()}>
                {adding ? "Adding to your books…" : "Add to my books"}
              </Button>
            )}
          </div>
          <p className="fl-sheet-note">
            Free to keep.{" "}
            {detail && (
              <a href={detail.source_url} target="_blank" rel="noreferrer">
                From Project Gutenberg
              </a>
            )}
          </p>
        </div>
      </div>
    </div>
  );
}

export function FreeLibrary({ onBack, onOpen }: { onBack: () => void; onOpen: (bookId: string) => void }) {
  const [filters, setFilters] = useState<Filters>(filtersFromLocation);
  const [summary, setSummary] = useState<ClassicsCatalog | null>(null);
  const [shelves, setShelves] = useState<{ language: string; shelves: ClassicShelf[] } | null>(null);
  const [results, setResults] = useState<{ key: string; matches: number; books: Classic[] } | null>(null);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const [added, setAdded] = useState<Record<string, string>>({});
  const [picked, setPicked] = useState<Classic | null>(null);
  const pickedFrom = useRef<HTMLElement | null>(null);
  const sentinel = useRef<HTMLLIElement>(null);
  const [attempt, setAttempt] = useState(0);

  const needle = filters.q.trim();
  const wandering = !needle && !filters.genre && !filters.mood;
  const key = [needle, filters.language, filters.genre, filters.mood].join("\n");
  const search = useMemo<ClassicsSearch>(
    () => ({ q: needle, language: filters.language, genre: filters.genre, mood: filters.mood }),
    [needle, filters.language, filters.genre, filters.mood],
  );

  useEffect(() => rememberFilters(filters), [filters]);

  // The catalogue's size, languages, genres and moods: asked for once.
  useEffect(() => {
    let stale = false;
    api
      .classics({ limit: 1 })
      .then((catalog) => {
        if (!stale) setSummary(catalog);
      })
      .catch(() => {
        if (!stale) setFailed(true);
      });
    return () => {
      stale = true;
    };
  }, [attempt]);

  useEffect(() => {
    let stale = false;
    setLoading(true);
    setFailed(false);
    const done = () => {
      if (!stale) setLoading(false);
    };
    const fail = () => {
      if (!stale) setFailed(true);
    };
    if (wandering) {
      const language = filters.language;
      api
        .classicsFront(language)
        .then((front) => {
          if (!stale) setShelves({ language, shelves: front.shelves });
        })
        .catch(fail)
        .finally(done);
      return () => {
        stale = true;
      };
    }
    const timer = window.setTimeout(() => {
      api
        .classics(search)
        .then((page) => {
          if (!stale) setResults({ key, matches: page.matches, books: page.books });
        })
        .catch(fail)
        .finally(done);
    }, SEARCH_DELAY_MS);
    return () => {
      stale = true;
      window.clearTimeout(timer);
    };
  }, [wandering, key, search, filters.language, attempt]);

  const current = results && results.key === key ? results : null;
  const hasMore = current !== null && current.books.length < current.matches;
  const fetchingMore = useRef(false);
  const loadMore = useCallback(() => {
    if (!current || fetchingMore.current || current.books.length >= current.matches) return;
    fetchingMore.current = true;
    const at = current.key;
    api
      .classics({ ...search, offset: current.books.length })
      .then((page) =>
        setResults((prev) => {
          if (!prev || prev.key !== at) return prev;
          const seen = new Set(prev.books.map((book) => book.id));
          return { ...prev, matches: page.matches, books: [...prev.books, ...page.books.filter((book) => !seen.has(book.id))] };
        }),
      )
      .catch(() => setFailed(true))
      .finally(() => {
        fetchingMore.current = false;
      });
  }, [current, search]);

  // Keep the grid filling as the reader nears its end; the button below it does the same by hand.
  useEffect(() => {
    const target = sentinel.current;
    if (!target || !hasMore || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver((entries) => entries.some((entry) => entry.isIntersecting) && loadMore(), {
      rootMargin: "600px 0px",
    });
    observer.observe(target);
    return () => observer.disconnect();
  }, [hasMore, loadMore]);

  const owned = useCallback((book: Classic) => Boolean(book.book_id ?? added[book.id]), [added]);
  const pick = (book: Classic) => {
    pickedFrom.current = document.activeElement as HTMLElement | null;
    setPicked(book);
  };
  const closeSheet = useCallback(() => {
    setPicked(null);
    pickedFrom.current?.focus();
  }, []);
  const set = (change: Partial<Filters>) => setFilters((prev) => ({ ...prev, ...change }));
  const clear = () => set({ q: "", genre: "", mood: "" });

  const moodLabel = summary?.moods.find((mood) => mood.id === filters.mood)?.label;
  const genreLabel = summary?.genres.find((genre) => genre.id === filters.genre)?.label;
  const heading = [moodLabel, genreLabel, needle ? `“${needle}”` : null].filter(Boolean).join(" · ") || "Everything";
  const languages = (summary?.languages ?? []).slice(0, LANGUAGE_CHOICES);
  const shownShelves = shelves && shelves.language === filters.language ? shelves.shelves : null;

  return (
    <div className="mothli-library fl">
      <a className="skip-link" href="#fl-main">
        Skip to the books
      </a>
      <header className="ml-top">
        <a
          className="ml-brand"
          href="/"
          aria-label="Mothli home"
          onClick={(event) => {
            event.preventDefault();
            onBack();
          }}
        >
          <MothliMark className="ml-brand-mark" size={38} />
          <span>Mothli</span>
        </a>
        <button type="button" className="fl-back" onClick={onBack}>
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="m15 5-7 7 7 7" />
          </svg>
          Your books
        </button>
      </header>

      <main id="fl-main">
        <section className="fl-hero">
          <p className="m-kicker">Free to keep</p>
          <h1>The Library</h1>
          <div className="ml-note fl-hero-note">
            <Moth size={40} />
            <p className="m-hand">
              {summary ? `${count(summary.total)} books, all out of copyright.` : "Thousands of books, all out of copyright."}{" "}
              Find one and I’ll read along.
            </p>
          </div>
          <div className="fl-find">
            <label className="fl-search">
              <Icon name="search" size={20} />
              <span className="sr-only">Search the library</span>
              <input
                type="search"
                placeholder="Search by title or author"
                value={filters.q}
                maxLength={120}
                onChange={(event) => set({ q: event.currentTarget.value })}
              />
            </label>
            {languages.length > 1 && (
              <label className="fl-language">
                <span className="sr-only">Language</span>
                <select value={filters.language} onChange={(event) => set({ language: event.currentTarget.value })}>
                  <option value="">Any language</option>
                  {languages.map((item) => (
                    <option key={item.code} value={item.code}>
                      {languageName(item.code)}
                    </option>
                  ))}
                </select>
              </label>
            )}
          </div>
        </section>

        {summary && (
          <nav className="fl-browse" aria-label="Browse the library">
            <ChipRow
              labelledBy="fl-moods-title"
              heading={
                <h2 id="fl-moods-title" className="m-hand fl-chips-title">
                  How are you feeling?
                </h2>
              }
            >
                {summary.moods.map((mood) => (
                  <li key={mood.id}>
                    <button
                      type="button"
                      className="fl-chip fl-chip--mood"
                      aria-pressed={filters.mood === mood.id}
                      onClick={() => set({ mood: filters.mood === mood.id ? "" : mood.id })}
                    >
                      {mood.label}
                    </button>
                  </li>
                ))}
            </ChipRow>
            <ChipRow
              labelledBy="fl-genres-title"
              heading={
                <h2 id="fl-genres-title" className="fl-chips-label">
                  Or pick a kind of book
                </h2>
              }
            >
                {summary.genres.map((genre) => (
                  <li key={genre.id}>
                    <button
                      type="button"
                      className="fl-chip"
                      aria-pressed={filters.genre === genre.id}
                      onClick={() => set({ genre: filters.genre === genre.id ? "" : genre.id })}
                    >
                      {genre.label}
                    </button>
                  </li>
                ))}
            </ChipRow>
          </nav>
        )}

        {failed && (
          <div className="fl-error fl-error--page" role="alert">
            <span>Couldn’t reach the library just now.</span>
            <Button onClick={() => setAttempt((value) => value + 1)}>Try again</Button>
          </div>
        )}

        {wandering ? (
          <div className="fl-shelves" aria-busy={loading && !shownShelves}>
            {shownShelves
              ? shownShelves.map((shelf) => (
                  <ShelfRow
                    key={`${shelf.kind}-${shelf.id}`}
                    shelf={shelf}
                    owned={owned}
                    onPick={pick}
                    onSeeAll={
                      shelf.kind === "mood"
                        ? () => set({ mood: shelf.id })
                        : shelf.kind === "genre"
                          ? () => set({ genre: shelf.id })
                          : undefined
                    }
                  />
                ))
              : !failed && (
                  <section className="fl-shelf" aria-hidden="true">
                    <header className="fl-shelf-head">
                      <h2>Most read right now</h2>
                    </header>
                    <ul className="fl-row">
                      <Ghosts />
                    </ul>
                  </section>
                )}
          </div>
        ) : (
          <section className="fl-results" aria-labelledby="fl-results-title">
            <header className="fl-results-head">
              <div>
                <h2 id="fl-results-title">{heading}</h2>
                <p aria-live="polite">
                  {current ? `${count(current.matches)} ${current.matches === 1 ? "book" : "books"}` : failed ? "" : "Looking…"}
                </p>
              </div>
              <button type="button" className="fl-link" onClick={clear}>
                Back to the shelves
              </button>
            </header>
            <ul className="fl-grid" aria-label="Books" aria-busy={!current}>
              {current ? (
                current.books.map((book) => (
                  <li key={book.id}>
                    <BookCard book={book} onShelf={owned(book)} onPick={pick} />
                  </li>
                ))
              ) : (
                !failed && <Ghosts />
              )}
              {hasMore && <li ref={sentinel} className="fl-sentinel" aria-hidden="true" />}
            </ul>
            {current && current.books.length === 0 && (
              <div className="fl-empty">
                <Moth size={56} />
                <p className="m-hand">I couldn’t find that one. Try fewer words, or another spelling.</p>
                <Button onClick={clear}>Back to the shelves</Button>
              </div>
            )}
            {hasMore && (
              <div className="fl-more">
                <Button onClick={loadMore}>Show more</Button>
              </div>
            )}
          </section>
        )}
      </main>

      <footer className="fl-foot">
        <p>
          Books from{" "}
          <a href={summary?.source_url ?? "https://www.gutenberg.org/"} target="_blank" rel="noreferrer">
            Project Gutenberg
          </a>
          . They are in the public domain in the United States; if you live elsewhere, check your country’s rules. A
          book is downloaded only when you add it.
        </p>
      </footer>

      {picked && (
        <BookSheet
          book={picked}
          shelfId={picked.book_id ?? added[picked.id] ?? null}
          onAdded={(book, bookId) => setAdded((prev) => ({ ...prev, [book.id]: bookId }))}
          onOpen={onOpen}
          onClose={closeSheet}
        />
      )}
    </div>
  );
}
