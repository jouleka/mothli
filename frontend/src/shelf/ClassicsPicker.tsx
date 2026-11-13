/** Pick a classic: a sheet of public-domain books a reader can add with one tap. The list is a
 * curated catalogue served by the reader's own server; a book is only downloaded when it is added. */
import { useEffect, useMemo, useRef, useState } from "react";
import type { Book, Classic, ClassicsCatalog } from "../api";
import { Button } from "../ui/Button";
import { Icon } from "../ui/Icon";
import { Moth } from "../ui/Moth";
import { BookCover } from "./Shelf";

export function ClassicsPicker({
  catalog,
  busyId,
  error,
  onAdd,
  onOpen,
  onClose,
}: {
  catalog: ClassicsCatalog;
  busyId: string | null;
  error: string | null;
  onAdd: (classic: Classic) => void;
  onOpen: (bookId: string) => void;
  onClose: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [query, setQuery] = useState("");

  useEffect(() => {
    ref.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const shown = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase();
    return needle
      ? catalog.books.filter((book) => `${book.title} ${book.author}`.toLocaleLowerCase().includes(needle))
      : catalog.books;
  }, [catalog.books, query]);

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
            These are long out of copyright, so they’re yours to keep. Pick one and I’ll read along.
          </p>
        </div>
        <label className="ml-search ml-classics-search">
          <Icon name="search" size={16} />
          <span className="sr-only">Search the classics</span>
          <input
            type="search"
            placeholder="Find a title or author"
            value={query}
            onChange={(event) => setQuery(event.currentTarget.value)}
          />
        </label>
        {error && (
          <p className="ml-classics-error" role="alert">
            {error}
          </p>
        )}
        <ul className="ml-classics-list" aria-label="Classics">
          {shown.map((classic) => {
            const adding = busyId === classic.id;
            const cover: Book = { book_id: classic.id, title: classic.title, author: classic.author };
            return (
              <li key={classic.id}>
                <BookCover book={cover} className="ml-classics-cover" />
                <div className="ml-classics-text">
                  <strong>{classic.title}</strong>
                  <span>
                    {classic.author}
                    {classic.year ? `, ${classic.year}` : ""}
                  </span>
                </div>
                {classic.book_id ? (
                  <Button aria-label={`Open ${classic.title}`} onClick={() => onOpen(classic.book_id!)}>
                    On your shelf
                  </Button>
                ) : (
                  <Button
                    variant="primary"
                    aria-label={adding ? `Adding ${classic.title}` : `Add ${classic.title}`}
                    aria-busy={adding}
                    disabled={busyId !== null}
                    onClick={() => onAdd(classic)}
                  >
                    {adding ? "Adding…" : "Add"}
                  </Button>
                )}
              </li>
            );
          })}
          {shown.length === 0 && <li className="ml-classics-none">Nothing by that name here.</li>}
        </ul>
        <p className="ml-classics-foot">
          From {catalog.source}. A book is downloaded only when you add it.
        </p>
      </div>
    </div>
  );
}
