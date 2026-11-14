/** Your reading history: what a reader has read, means to read and marked in other reading apps,
 * brought in from the files those apps let them export. A title here is a record, not a book; when
 * the free library has the book, it can be added from here. */
import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Book, type HistoryDetail, type HistoryEntry, type HistoryPage } from "../api";
import { Cover } from "../library/FreeLibrary";
import { BookCover } from "../shelf/Shelf";
import { Button } from "../ui/Button";
import { Icon } from "../ui/Icon";
import { Moth } from "../ui/Moth";
import { MothliMark } from "../ui/MothliMark";
import {
  parseHistoryFile,
  SOURCE_NAMES,
  summarize,
  UnknownHistoryFile,
  type ImportSource,
  type ParsedHistory,
  type ReadStatus,
} from "./parsers";
import "../library/free-library.css";
import "./reading-history.css";

const MAX_FILE_BYTES = 60 * 1024 * 1024;
const BATCH_BOOKS = 400;
const BATCH_HIGHLIGHTS = 800;
const PAGE = 60;
const TYPING_DELAY_MS = 160;

const STATUS_NAMES: Record<ReadStatus, string> = { read: "Read", reading: "Reading", want: "Want to read", abandoned: "Set aside" };
type View = "" | ReadStatus | "highlights" | "free";
const count = (n: number) => n.toLocaleString("en");
const plural = (n: number, one: string, many = `${one}s`) => `${count(n)} ${n === 1 ? one : many}`;

function longDate(day: string | null) {
  if (!day) return null;
  const at = new Date(`${day.slice(0, 10)}T12:00:00`);
  return Number.isNaN(at.getTime()) ? null : at.toLocaleDateString("en", { day: "numeric", month: "short", year: "numeric" });
}

function readFile(file: File): Promise<string> {
  if (typeof file.text === "function") return file.text();
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result ?? ""));
    reader.onerror = () => reject(reader.error);
    reader.readAsText(file);
  });
}

function Stars({ rating }: { rating: number }) {
  const whole = Math.round(rating);
  return (
    <span className="rh-stars" role="img" aria-label={`${rating} out of 5 stars`}>
      {"★".repeat(whole)}
      <i aria-hidden="true">{"★".repeat(5 - whole)}</i>
    </span>
  );
}

function Thumb({ entry }: { entry: HistoryEntry }) {
  if (entry.classic_id) {
    return <Cover book={{ id: entry.classic_id, title: entry.title, author: entry.author, language: "en", book_id: entry.book_id }} />;
  }
  const cloth: Book = { book_id: `history-${entry.id}`, title: entry.title, author: entry.author };
  return (
    <span className="fl-cover">
      <BookCover book={cloth} />
    </span>
  );
}

const GUIDES: { source: ImportSource; steps: string }[] = [
  {
    source: "goodreads",
    steps: "On the Goodreads website (not the app): My Books, then Tools, then “Import and export”, then “Export Library”. Bring the goodreads_library_export.csv file it gives you.",
  },
  {
    source: "storygraph",
    steps: "On StoryGraph: Manage Account, then “Export StoryGraph Library”. A link to the file arrives by email.",
  },
  {
    source: "kindle",
    steps: "Plug your Kindle into a computer with its cable. Open the Kindle drive, then the “documents” folder, and bring the file called “My Clippings.txt”. It holds every highlight and note made on that device.",
  },
  {
    source: "libby",
    steps: "In Libby: open a book’s Reading Journey and choose “Export Reading Data”, or open your Timeline and choose Actions, then “Export Timeline”. Pick “Data”.",
  },
];

type Chosen = { name: string; parsed?: ParsedHistory; problem?: string };
type Progress = { done: number; total: number };
type Outcome = { books: number; updated: number; highlights: number; skipped: number };

function Importer({ onImported, startOpen }: { onImported: () => Promise<number>; startOpen: boolean }) {
  const input = useRef<HTMLInputElement>(null);
  const [open, setOpen] = useState(startOpen);
  // with nothing imported (at first, or after removing it all) the import desk is the page
  useEffect(() => {
    if (startOpen) setOpen(true);
  }, [startOpen]);
  const [chosen, setChosen] = useState<Chosen[]>([]);
  const [over, setOver] = useState(false);
  const [progress, setProgress] = useState<Progress | null>(null);
  const [outcome, setOutcome] = useState<(Outcome & { free: number }) | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  const ready = chosen.flatMap((file) => (file.parsed ? [file.parsed] : []));
  const totals = summarize(ready);

  const choose = async (files: FileList | File[] | null) => {
    const list = Array.from(files ?? []);
    if (!list.length) return;
    setOutcome(null);
    setProblem(null);
    const read = await Promise.all(
      list.map(async (file): Promise<Chosen> => {
        if (file.size > MAX_FILE_BYTES) return { name: file.name, problem: "This file is too large to be a reading history." };
        try {
          const parsed = parseHistoryFile(file.name, await readFile(file));
          if (!parsed.books.length && !parsed.highlights.length) return { name: file.name, problem: "There is nothing in this file to bring in." };
          return { name: file.name, parsed };
        } catch (error) {
          return {
            name: file.name,
            problem: error instanceof UnknownHistoryFile ? error.message : "We couldn’t read this file.",
          };
        }
      }),
    );
    setChosen(read);
  };

  const bringIn = async () => {
    const total = totals.books + totals.highlights;
    const sum: Outcome = { books: 0, updated: 0, highlights: 0, skipped: 0 };
    let done = 0;
    setProblem(null);
    setProgress({ done, total });
    try {
      for (const file of ready) {
        for (let at = 0; at < file.books.length; at += BATCH_BOOKS) {
          const books = file.books.slice(at, at + BATCH_BOOKS);
          const result = await api.importHistory({ source: file.source, books, highlights: [] });
          sum.books += result.books_added;
          sum.updated += result.books_updated;
          sum.skipped += result.skipped;
          done += books.length;
          setProgress({ done, total });
        }
        for (let at = 0; at < file.highlights.length; at += BATCH_HIGHLIGHTS) {
          const highlights = file.highlights.slice(at, at + BATCH_HIGHLIGHTS);
          const result = await api.importHistory({ source: file.source, books: [], highlights });
          sum.books += result.books_added;
          sum.highlights += result.highlights_added;
          sum.skipped += result.skipped;
          done += highlights.length;
          setProgress({ done, total });
        }
      }
      const free = await onImported();
      setOutcome({ ...sum, free });
      setChosen([]);
    } catch {
      setProblem("The import stopped part-way. What was brought in so far is kept; you can run it again safely.");
      await onImported().catch(() => 0);
    } finally {
      setProgress(null);
    }
  };

  if (!open) {
    return (
      <div className="rh-reopen">
        <Button icon="plus" onClick={() => setOpen(true)}>
          Import more
        </Button>
      </div>
    );
  }

  return (
    <section className="rh-import" aria-labelledby="rh-import-title">
      <h2 id="rh-import-title">Bring in your files</h2>
      <div
        className="rh-drop"
        data-over={over}
        onDragOver={(event) => {
          event.preventDefault();
          setOver(true);
        }}
        onDragLeave={() => setOver(false)}
        onDrop={(event) => {
          event.preventDefault();
          setOver(false);
          void choose(event.dataTransfer.files);
        }}
      >
        <Icon name="download" size={26} />
        <p className="rh-drop-title">Drop your export files here</p>
        <p className="rh-drop-hint">Goodreads · StoryGraph · Kindle highlights · Libby</p>
        <Button variant="primary" disabled={progress !== null} onClick={() => input.current?.click()}>
          Choose files
        </Button>
        <input
          ref={input}
          className="sr-only"
          type="file"
          multiple
          accept=".csv,.txt,.json,text/csv,text/plain,application/json"
          aria-label="Choose export files"
          onChange={(event) => {
            void choose(event.currentTarget.files);
            event.currentTarget.value = "";
          }}
        />
      </div>

      {chosen.length > 0 && (
        <ul className="rh-files" aria-label="Chosen files">
          {chosen.map((file, index) => (
            <li key={`${file.name}-${index}`} data-bad={!file.parsed}>
              <strong>{file.name}</strong>
              {file.parsed ? (
                <span>
                  {SOURCE_NAMES[file.parsed.source]}:{" "}
                  {[
                    file.parsed.books.length ? plural(file.parsed.books.length, "book") : null,
                    file.parsed.highlights.length ? plural(file.parsed.highlights.length, "highlight") : null,
                  ]
                    .filter(Boolean)
                    .join(" and ")}
                </span>
              ) : (
                <span>{file.problem}</span>
              )}
            </li>
          ))}
        </ul>
      )}

      {ready.length > 0 && (
        <div className="rh-go">
          <Button variant="primary" aria-busy={progress !== null} disabled={progress !== null} onClick={() => void bringIn()}>
            {progress
              ? `Importing… ${count(progress.done)} of ${count(progress.total)}`
              : `Import ${[totals.books ? plural(totals.books, "book") : null, totals.highlights ? plural(totals.highlights, "highlight") : null]
                  .filter(Boolean)
                  .join(" and ")}`}
          </Button>
          {progress && <progress value={progress.done} max={Math.max(progress.total, 1)} aria-label="Import progress" />}
        </div>
      )}

      {problem && (
        <p className="fl-error" role="alert">
          {problem}
        </p>
      )}
      {outcome && (
        <p className="rh-outcome" role="status">
          <Icon name="check" size={16} />
          <span>
            {outcome.books || outcome.highlights
              ? `Brought in ${[outcome.books ? plural(outcome.books, "new book") : null, outcome.highlights ? plural(outcome.highlights, "new highlight") : null]
                  .filter(Boolean)
                  .join(" and ")}.`
              : "Everything in those files was already here."}
            {outcome.updated > 0 && ` Filled in details for ${plural(outcome.updated, "book")} you already had.`}
            {outcome.free > 0 && ` ${plural(outcome.free, "book")} on your list ${outcome.free === 1 ? "is" : "are"} free to read here.`}
            {outcome.skipped > 0 && ` ${plural(outcome.skipped, "item")} could not be kept.`}
          </span>
        </p>
      )}

      <details className="rh-guides">
        <summary>How do I get my files?</summary>
        <dl>
          {GUIDES.map((guide) => (
            <div key={guide.source}>
              <dt>{SOURCE_NAMES[guide.source]}</dt>
              <dd>{guide.steps}</dd>
            </div>
          ))}
        </dl>
      </details>
      <p className="rh-honest">
        This brings your list, ratings, reviews, highlights and notes. Books bought in Kindle, Apple Books and similar
        stores are copy-protected, so the books themselves can’t be moved. Nothing leaves your own Mothli.
      </p>
    </section>
  );
}

function EntrySheet({
  entry,
  onOpen,
  onAdded,
  onRemoved,
  onClose,
}: {
  entry: HistoryEntry;
  onOpen: (bookId: string) => void;
  onAdded: (entry: HistoryEntry, bookId: string) => void;
  onRemoved: (entry: HistoryEntry) => void;
  onClose: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [detail, setDetail] = useState<HistoryDetail | null>(null);
  const [adding, setAdding] = useState(false);
  const [sure, setSure] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  useEffect(() => {
    ref.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
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
      .historyEntry(entry.id)
      .then((found) => {
        if (!stale) setDetail(found);
      })
      .catch(() => {
        if (!stale) setProblem("Couldn’t load this book’s details.");
      });
    return () => {
      stale = true;
    };
  }, [entry.id]);

  const add = async () => {
    if (!entry.classic_id) return;
    setAdding(true);
    setProblem(null);
    try {
      const added = await api.addClassic(entry.classic_id);
      onAdded(entry, added.book_id);
    } catch (error) {
      setProblem(error instanceof Error && error.message ? error.message : "Couldn’t add this book. Try again.");
    } finally {
      setAdding(false);
    }
  };

  const facts = [
    entry.status ? STATUS_NAMES[entry.status] : null,
    longDate(entry.finished_on),
    entry.sources.map((source) => SOURCE_NAMES[source as ImportSource] ?? source).join(", "),
  ].filter(Boolean);

  return (
    <div className="fl-scrim" onPointerDown={(event) => event.target === event.currentTarget && onClose()}>
      <div ref={ref} className="fl-sheet rh-sheet" role="dialog" aria-modal="true" aria-labelledby="rh-sheet-title" tabIndex={-1}>
        <button className="fl-sheet-close" aria-label="Close" onClick={onClose}>
          <Icon name="close" size={18} />
        </button>
        <div className="fl-sheet-cover">
          <Thumb entry={entry} />
        </div>
        <div className="fl-sheet-body">
          <h2 id="rh-sheet-title">{entry.title}</h2>
          {entry.author && <p className="fl-sheet-author">{entry.author}</p>}
          {facts.length > 0 && <p className="fl-sheet-facts">{facts.join(" · ")}</p>}
          {entry.rating && (
            <p className="rh-sheet-rating">
              <Stars rating={entry.rating} />
            </p>
          )}
          <div className="fl-sheet-actions rh-sheet-actions">
            {entry.book_id ? (
              <Button variant="primary" onClick={() => onOpen(entry.book_id!)}>
                {entry.status === "read" ? "Read it again" : "Start reading"}
              </Button>
            ) : entry.classic_id ? (
              <Button variant="primary" aria-busy={adding} disabled={adding} onClick={() => void add()}>
                {adding ? "Adding to your books…" : "Add it free"}
              </Button>
            ) : (
              <p className="rh-none">
                This one isn’t in the free Library. If you have your own copy without copy protection, add it from your
                shelf with “Add a book”.
              </p>
            )}
          </div>
          {problem && (
            <p className="fl-error" role="alert">
              {problem}
            </p>
          )}
          {detail?.review && (
            <section className="rh-part" aria-label="Your review">
              <h3>Your review</h3>
              <p className="rh-review">{detail.review}</p>
            </section>
          )}
          {detail && detail.marks.length > 0 && (
            <section className="rh-part" aria-label="Your highlights and notes">
              <h3>Your highlights and notes · {count(detail.marks.length)}</h3>
              <ol className="rh-marks">
                {detail.marks.map((mark) => (
                  <li key={mark.id}>
                    {mark.text && <blockquote>{mark.text}</blockquote>}
                    {mark.note && <p className="m-hand rh-note">{mark.note}</p>}
                    <p className="rh-mark-where">
                      {[mark.location, longDate(mark.made_at), SOURCE_NAMES[mark.source as ImportSource] ?? mark.source]
                        .filter(Boolean)
                        .join(" · ")}
                    </p>
                  </li>
                ))}
              </ol>
            </section>
          )}
          <div className="rh-remove">
            {sure ? (
              <>
                <span>Remove this title and its highlights from your history?</span>
                <Button
                  variant="danger"
                  onClick={() => {
                    void api
                      .forgetHistory(entry.id)
                      .then(() => onRemoved(entry))
                      .catch(() => setProblem("Couldn’t remove it. Try again."));
                  }}
                >
                  Yes, remove
                </Button>
                <Button onClick={() => setSure(false)}>Keep it</Button>
              </>
            ) : (
              <button type="button" className="fl-link" onClick={() => setSure(true)}>
                Remove from my history
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

export function ReadingHistory({ onBack, onOpen }: { onBack: () => void; onOpen: (bookId: string) => void }) {
  const [page, setPage] = useState<HistoryPage | null>(null);
  const [view, setView] = useState<View>("");
  const [query, setQuery] = useState("");
  const [failed, setFailed] = useState(false);
  const [picked, setPicked] = useState<HistoryEntry | null>(null);
  const pickedFrom = useRef<HTMLElement | null>(null);
  const [forgetting, setForgetting] = useState(false);
  const typed = useRef(query);
  const [turn, setTurn] = useState(0);

  const needle = query.trim();
  const search = useCallback(
    (offset = 0) =>
      api.history({
        status: view === "highlights" || view === "free" ? "" : view,
        only: view === "highlights" || view === "free" ? view : "",
        q: needle,
        offset,
        limit: PAGE,
      }),
    [view, needle],
  );

  useEffect(() => {
    let stale = false;
    const wasTyping = typed.current !== needle;
    typed.current = needle;
    const timer = window.setTimeout(
      () => {
        search()
          .then((loaded) => {
            if (stale) return;
            setFailed(false);
            setPage(loaded);
          })
          .catch(() => {
            if (!stale) setFailed(true);
          });
      },
      wasTyping ? TYPING_DELAY_MS : 0,
    );
    return () => {
      stale = true;
      window.clearTimeout(timer);
    };
  }, [search, needle, turn]);

  const reload = useCallback(async () => {
    const loaded = await search();
    setPage(loaded);
    return loaded.free;
  }, [search]);

  const more = () => {
    if (!page) return;
    void search(page.entries.length)
      .then((next) => setPage((prev) => (prev ? { ...next, entries: [...prev.entries, ...next.entries] } : next)))
      .catch(() => setFailed(true));
  };

  const pick = (entry: HistoryEntry) => {
    pickedFrom.current = document.activeElement as HTMLElement | null;
    setPicked(entry);
  };
  const closeSheet = useCallback(() => {
    setPicked(null);
    pickedFrom.current?.focus();
  }, []);

  const empty = page !== null && page.books === 0;
  const views: { id: View; label: string; n: number }[] = page
    ? [
        { id: "", label: "All", n: page.books },
        { id: "read", label: STATUS_NAMES.read, n: page.by_status.read },
        { id: "reading", label: STATUS_NAMES.reading, n: page.by_status.reading },
        { id: "want", label: STATUS_NAMES.want, n: page.by_status.want },
        { id: "abandoned", label: STATUS_NAMES.abandoned, n: page.by_status.abandoned },
        { id: "highlights", label: "With highlights", n: page.with_highlights },
        { id: "free", label: "Free to read here", n: page.free },
      ]
    : [];

  return (
    <div className="mothli-library fl rh">
      <a className="skip-link" href="#rh-main">
        Skip to your reading history
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

      <main id="rh-main">
        <section className="fl-hero rh-hero">
          <p className="m-kicker">From your other reading apps</p>
          <h1>Your reading history</h1>
          <div className="ml-note fl-hero-note">
            <Moth size={40} />
            <p className="m-hand">
              {page && !empty
                ? `${plural(page.books, "book")} and ${plural(page.highlights, "highlight")} so far. I’ll keep them safe.`
                : "Bring what you’ve read elsewhere. I’ll keep your list and everything you marked."}
            </p>
          </div>
        </section>

        {page !== null && <Importer startOpen={empty} onImported={reload} />}

        {failed && (
          <div className="fl-error fl-error--page" role="alert">
            <span>Couldn’t reach your reading history just now.</span>
            <Button onClick={() => setTurn((value) => value + 1)}>Try again</Button>
          </div>
        )}

        {page && !empty && (
          <section className="rh-list" aria-labelledby="rh-list-title">
            <h2 id="rh-list-title" className="sr-only">
              Your books
            </h2>
            <div className="rh-tools">
              <ul className="rh-views" aria-label="Show">
                {views
                  .filter((item) => item.id === "" || item.n > 0)
                  .map((item) => (
                    <li key={item.id || "all"}>
                      <button type="button" className="fl-chip" aria-pressed={view === item.id} onClick={() => setView(item.id)}>
                        {item.label} <span className="rh-n">{count(item.n)}</span>
                      </button>
                    </li>
                  ))}
              </ul>
              <label className="fl-search rh-search">
                <Icon name="search" size={18} />
                <span className="sr-only">Search your reading history</span>
                <input
                  type="search"
                  placeholder="Find a title or author"
                  value={query}
                  maxLength={120}
                  onChange={(event) => setQuery(event.currentTarget.value)}
                />
              </label>
            </div>

            <ul className="rh-rows" aria-label="Books in your history">
              {page.entries.map((entry) => (
                <li key={entry.id}>
                  <button type="button" className="rh-row" onClick={() => pick(entry)} aria-label={`${entry.title}${entry.author ? `, ${entry.author}` : ""}`}>
                    <span className="rh-thumb">
                      <Thumb entry={entry} />
                    </span>
                    <span className="rh-row-text">
                      <span className="rh-row-title">{entry.title}</span>
                      {entry.author && <span className="rh-row-author">{entry.author}</span>}
                      <span className="rh-row-facts">
                        {entry.status && <span>{STATUS_NAMES[entry.status]}{entry.finished_on ? ` ${longDate(entry.finished_on)}` : ""}</span>}
                        {entry.rating ? <Stars rating={entry.rating} /> : null}
                        {entry.highlights > 0 && <span>{plural(entry.highlights, "highlight")}</span>}
                      </span>
                    </span>
                    {entry.book_id ? (
                      <span className="rh-tag rh-tag--shelf">
                        <Icon name="check" size={12} /> On your shelf
                      </span>
                    ) : entry.classic_id ? (
                      <span className="rh-tag rh-tag--free">Free to read here</span>
                    ) : null}
                  </button>
                </li>
              ))}
            </ul>
            {page.entries.length === 0 && <p className="rh-nothing">Nothing here matches.</p>}
            {page.entries.length < page.matches && (
              <div className="fl-more">
                <Button onClick={more}>Show more</Button>
              </div>
            )}

            <div className="rh-forget">
              {forgetting ? (
                <>
                  <span>Remove everything you imported? Your Mothli books and notes are not touched.</span>
                  <Button
                    variant="danger"
                    onClick={() => {
                      void api
                        .forgetHistory()
                        .then(() => {
                          setForgetting(false);
                          setView("");
                          setQuery("");
                          setTurn((value) => value + 1);
                        })
                        .catch(() => setFailed(true));
                    }}
                  >
                    Yes, remove it all
                  </Button>
                  <Button onClick={() => setForgetting(false)}>Keep it</Button>
                </>
              ) : (
                <button type="button" className="fl-link" onClick={() => setForgetting(true)}>
                  Remove everything I imported
                </button>
              )}
            </div>
          </section>
        )}
      </main>

      {picked && (
        <EntrySheet
          entry={picked}
          onOpen={onOpen}
          onAdded={(entry, bookId) => {
            const shelved = { ...entry, book_id: bookId };
            setPicked(shelved);
            setPage((prev) => (prev ? { ...prev, entries: prev.entries.map((item) => (item.id === entry.id ? shelved : item)) } : prev));
          }}
          onRemoved={() => {
            closeSheet();
            setTurn((value) => value + 1);
          }}
          onClose={closeSheet}
        />
      )}
    </div>
  );
}
