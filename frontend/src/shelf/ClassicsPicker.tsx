/** Pick a classic: a sheet for finding a public-domain book and adding it with one tap. The
 * catalogue lives on the reader's own server, which searches it; a book is only downloaded when it
 * is added. */
import { useEffect, useMemo, useRef, useState } from "react";
import { api, type Book, type Classic, type ClassicsCatalog } from "../api";
import { Button } from "../ui/Button";
import { Icon } from "../ui/Icon";
import { Moth } from "../ui/Moth";
import { BookCover } from "./Shelf";

const SEARCH_DELAY_MS = 250;
const LANGUAGE_CHOICES = 40;

function languageName(code: string) {
  try {
    return new Intl.DisplayNames(["en"], { type: "language" }).of(code) ?? code;
  } catch {
    return code;
  }
}

const count = (n: number) => n.toLocaleString("en");

export function ClassicsPicker({
  catalog,
  busyId,
  error,
  onAdd,
  onOpen,
  onClose,
}: {
  /** The opening page: the most-read books, with the catalogue's size and languages. */
  catalog: ClassicsCatalog;
  busyId: string | null;
  error: string | null;
  /** Adds the book and resolves to its id on the shelf, or null when it could not be added. */
  onAdd: (classic: Classic) => Promise<string | null>;
  onOpen: (bookId: string) => void;
  onClose: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [query, setQuery] = useState("");
  const [language, setLanguage] = useState("");
  const [found, setFound] = useState<{ key: string; matches: number; books: Classic[] } | null>(null);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [added, setAdded] = useState<Record<string, string>>({});

  useEffect(() => {
    ref.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const needle = query.trim();
  const key = `${language}\n${needle}`;
  const browsing = !needle && !language;

  useEffect(() => {
    setSearchError(null);
    if (browsing) {
      setSearching(false);
      return;
    }
    let stale = false;
    setSearching(true);
    const timer = window.setTimeout(() => {
      api
        .classics({ q: needle, language })
        .then((page) => {
          if (!stale) setFound({ key, matches: page.matches, books: page.books });
        })
        .catch(() => {
          if (!stale) setSearchError("Couldn’t search just now. Try again.");
        })
        .finally(() => {
          if (!stale) setSearching(false);
        });
    }, SEARCH_DELAY_MS);
    return () => {
      stale = true;
      window.clearTimeout(timer);
    };
  }, [browsing, key, language, needle]);

  // While a new search is on its way, the previous list stays up rather than flashing empty.
  const [more, setMore] = useState<{ key: string; books: Classic[] }>({ key: "", books: [] });
  const base = browsing ? { key, matches: catalog.matches, books: catalog.books } : found;
  const extra = base && more.key === base.key ? more.books : [];
  const shown = useMemo(() => [...(base?.books ?? []), ...extra], [base, extra]);
  const matches = base?.matches ?? 0;
  const settled = base !== null && base.key === key && !searching;

  const loadMore = () => {
    if (!base || searching) return;
    const at = base.key;
    setSearching(true);
    api
      .classics({ q: needle, language, offset: shown.length })
      .then((page) => setMore((prev) => ({ key: at, books: [...(prev.key === at ? prev.books : []), ...page.books] })))
      .catch(() => setSearchError("Couldn’t load more just now. Try again."))
      .finally(() => setSearching(false));
  };

  const languages = catalog.languages.slice(0, LANGUAGE_CHOICES);

  return (
    <div className="ml-classics-scrim" onPointerDown={(event) => event.target === event.currentTarget && onClose()}>
      <div ref={ref} className="ml-classics" role="dialog" aria-modal="true" aria-labelledby="classics-title" tabIndex={-1}>
        <header className="ml-classics-head">
          <div>
            <p className="m-kicker">Free to read</p>
            <h2 id="classics-title">Pick a classic</h2>
          </div>
          <button className="ml-guide-close" aria-label="Close classics" onClick={onClose}>
            <Icon name="close" size={18} />
          </button>
        </header>
        <div className="ml-note">
          <Moth size={40} />
          <p className="m-hand ml-classics-note">
            {count(catalog.total)} books, out of copyright and yours to keep. Pick one and I’ll read along.
          </p>
        </div>
        <div className="ml-classics-find">
          <label className="ml-search ml-classics-search">
            <Icon name="search" size={16} />
            <span className="sr-only">Search the classics</span>
            <input
              type="search"
              placeholder="Find a title or author"
              value={query}
              maxLength={120}
              onChange={(event) => setQuery(event.currentTarget.value)}
            />
          </label>
          {languages.length > 1 && (
            <label className="ml-classics-language">
              <span className="sr-only">Language</span>
              <select value={language} onChange={(event) => setLanguage(event.currentTarget.value)}>
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
        {(error || searchError) && (
          <p className="ml-classics-error" role="alert">
            {error || searchError}
          </p>
        )}
        <p className="ml-classics-count" aria-live="polite">
          {!settled
            ? "Looking…"
            : browsing
              ? "Most read right now"
              : `${count(matches)} ${matches === 1 ? "book" : "books"}`}
        </p>
        <ul className="ml-classics-list" aria-label="Classics" aria-busy={searching}>
          {shown.map((classic) => {
            const adding = busyId === classic.id;
            const shelfId = classic.book_id ?? added[classic.id] ?? null;
            const cover: Book = { book_id: classic.id, title: classic.title, author: classic.author };
            return (
              <li key={classic.id}>
                <BookCover book={cover} className="ml-classics-cover" />
                <div className="ml-classics-text">
                  <strong>{classic.title}</strong>
                  <span>
                    {classic.author}
                    {classic.language !== "en" ? ` · ${languageName(classic.language)}` : ""}
                  </span>
                </div>
                {shelfId ? (
                  <Button aria-label={`Open ${classic.title}`} onClick={() => onOpen(shelfId)}>
                    On your shelf
                  </Button>
                ) : (
                  <Button
                    variant="primary"
                    aria-label={adding ? `Adding ${classic.title}` : `Add ${classic.title}`}
                    aria-busy={adding}
                    disabled={busyId !== null}
                    onClick={() => {
                      void onAdd(classic).then((bookId) => {
                        if (bookId) setAdded((prev) => ({ ...prev, [classic.id]: bookId }));
                      });
                    }}
                  >
                    {adding ? "Adding…" : "Add"}
                  </Button>
                )}
              </li>
            );
          })}
          {settled && shown.length === 0 && <li className="ml-classics-none">Nothing by that name here.</li>}
          {settled && shown.length > 0 && shown.length < matches && (
            <li className="ml-classics-more">
              <Button onClick={loadMore}>Show more</Button>
            </li>
          )}
        </ul>
        <p className="ml-classics-foot">
          From{" "}
          <a href={catalog.source_url} target="_blank" rel="noreferrer">
            {catalog.source}
          </a>
          . Public domain in the United States; if you live elsewhere, check your country’s rules. A book is
          downloaded only when you add it.
        </p>
      </div>
    </div>
  );
}
