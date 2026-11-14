/** Your reading history: what a reader has read, means to read and marked in other reading apps,
 * brought in from the files those apps let them export. A title here is a record, not a book; when
 * the free library has the book, it can be added from here. */
import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import { api, type Book, type HistoryDetail, type HistoryEntry, type HistoryPage } from "../api";
import { BOOK_ACCEPT, BOOK_FORMATS, bookFileKind, prepareBookUpload, unsupportedBookMessage } from "../import/bookFiles";
import { Cover } from "../library/FreeLibrary";
import { BookCover } from "../shelf/Shelf";
import { Button } from "../ui/Button";
import { Icon } from "../ui/Icon";
import { Moth } from "../ui/Moth";
import { MothliMark } from "../ui/MothliMark";
import {
  isDatabase,
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
const MAX_DATABASE_BYTES = 500 * 1024 * 1024;
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

/** The first bytes of a file, to tell a database from an export without reading all of it. */
function readHead(file: File): Promise<Uint8Array> {
  const head = file.slice(0, 16);
  if (typeof head.arrayBuffer === "function") return head.arrayBuffer().then((buffer) => new Uint8Array(buffer));
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(new Uint8Array(reader.result as ArrayBuffer));
    reader.onerror = () => reject(reader.error);
    reader.readAsArrayBuffer(head);
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
  // the reader's own copy carries its own cover; a title with no book yet wears plain cloth
  const cloth: Book = { book_id: entry.book_id ?? `history-${entry.id}`, title: entry.title, author: entry.author };
  return (
    <span className="fl-cover">
      <BookCover book={cloth} art={entry.book_id ? `/api/books/${encodeURIComponent(entry.book_id)}/cover` : undefined} />
    </span>
  );
}

type Way = { title?: string; steps: string[] };
type Source = { id: string; name: string; brings: string; tone: string; ways: Way[]; note?: string };

/** Where a reader may be coming from, and exactly what to fetch from there. */
const SOURCES: Source[] = [
  {
    id: "goodreads",
    name: "Goodreads",
    brings: "Your list, ratings, reviews",
    tone: "oxblood",
    ways: [
      {
        steps: [
          "Open goodreads.com in a browser. The phone app can’t do this.",
          "Go to My Books, then Tools, then “Import and export”.",
          "Choose “Export Library” and wait for the link to appear.",
          "Bring the file it gives you: goodreads_library_export.csv.",
        ],
      },
    ],
  },
  {
    id: "kindle",
    name: "Kindle",
    brings: "Highlights and notes",
    tone: "navy",
    ways: [
      {
        title: "On a Kindle e-reader",
        steps: [
          "Plug the Kindle into a computer with its cable.",
          "Open the Kindle drive, then the “documents” folder.",
          "Bring the file called “My Clippings.txt”. It holds every highlight and note made on that device.",
        ],
      },
      {
        title: "In the Kindle app on a phone or tablet",
        steps: [
          "Open a book and tap its Notebook (the page icon).",
          "Choose Share or Export, and email it to yourself.",
          "Bring the .html file from that email. It is one file per book.",
        ],
      },
    ],
    note: "Most Kindle books are copy-protected and stay in Kindle. One that Amazon lets you download without protection can be dropped here like any other book.",
  },
  {
    id: "kobo",
    name: "Kobo",
    brings: "Library, progress, highlights",
    tone: "forest",
    ways: [
      {
        steps: [
          "Plug the Kobo into a computer.",
          "Turn on “show hidden files”, then open the folder called “.kobo” on the Kobo drive.",
          "Bring the file “KoboReader.sqlite”. It holds your library, what you finished, and everything you marked.",
        ],
      },
    ],
  },
  {
    id: "apple",
    name: "Apple Books",
    brings: "Library, progress, highlights",
    tone: "slate",
    ways: [
      {
        title: "On a Mac",
        steps: [
          "In Finder choose Go, then “Go to Folder”.",
          "Paste ~/Library/Containers/com.apple.iBooksX/Data/Documents",
          "Bring two files together: the .sqlite file inside “BKLibrary” and the .sqlite file inside “AEAnnotation”.",
        ],
      },
    ],
    note: "Apple gives no way to do this from an iPhone or iPad. Books you added to Apple Books yourself can be dragged out of it and dropped here.",
  },
  {
    id: "storygraph",
    name: "StoryGraph",
    brings: "Your list, ratings, reviews",
    tone: "plum",
    ways: [
      {
        steps: [
          "On StoryGraph open Manage Account.",
          "Choose “Export StoryGraph Library”.",
          "A link to the file arrives by email. Bring that file.",
        ],
      },
    ],
  },
  {
    id: "readwise",
    name: "Readwise",
    brings: "Every highlight, in one go",
    tone: "ochre",
    ways: [
      {
        steps: [
          "Open readwise.io/access_token and copy your access token.",
          "Paste it in the box below. It is used once and not kept.",
        ],
      },
    ],
    note: "Prefer a file? On Readwise choose Export, then CSV, and drop it here instead.",
  },
  {
    id: "libby",
    name: "Libby",
    brings: "Highlights and loans",
    tone: "forest",
    ways: [
      {
        steps: [
          "For one book: open its Reading Journey and choose “Export Reading Data”.",
          "For everything you borrowed: open your Timeline, choose Actions, then “Export Timeline”.",
          "Pick “Data” either way, and bring the file.",
        ],
      },
    ],
  },
  {
    id: "other",
    name: "Somewhere else",
    brings: "KOReader, Calibre, your own list",
    tone: "slate",
    ways: [
      {
        title: "KOReader",
        steps: ["In a book open the top menu, the tools tab, then “Export highlights”.", "Export as JSON and bring that file."],
      },
      {
        title: "Calibre, Hardcover, BookWyrm, LibraryThing, or a list you keep",
        steps: [
          "Export or save your books as a CSV file.",
          "Any spreadsheet works if it has a column for the title and one for the author. Shelves, ratings and dates are read when they are there.",
        ],
      },
    ],
    note: "Google Play Books isn’t possible yet: it keeps your notes as Google Docs, which we can’t read reliably so far.",
  },
];

type Chosen = { name: string; parsed?: ParsedHistory; database?: File; book?: File; problem?: string };
type Progress = { done: number; total: number };
type Outcome = { books: number; updated: number; highlights: number; skipped: number; shelved?: number };

function Importer({ onImported, onClose }: { onImported: () => Promise<number>; onClose?: () => void }) {
  const input = useRef<HTMLInputElement>(null);
  const [from, setFrom] = useState<string | null>(null);
  const [chosen, setChosen] = useState<Chosen[]>([]);
  const [over, setOver] = useState(false);
  const [progress, setProgress] = useState<Progress | null>(null);
  const [outcome, setOutcome] = useState<(Outcome & { free: number }) | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  const ready = chosen.flatMap((file) => (file.parsed ? [file.parsed] : []));
  const databases = chosen.flatMap((file) => (file.database ? [file.database] : []));
  const bookFiles = chosen.flatMap((file) => (file.book ? [file.book] : []));
  const totals = summarize(ready);
  const [token, setToken] = useState("");
  const [asking, setAsking] = useState(false);

  const choose = async (files: FileList | File[] | null) => {
    const list = Array.from(files ?? []);
    if (!list.length) return;
    setOutcome(null);
    setProblem(null);
    const read = await Promise.all(
      list.map(async (file): Promise<Chosen> => {
        try {
          if (isDatabase(await readHead(file))) {
            return file.size > MAX_DATABASE_BYTES
              ? { name: file.name, problem: "This database is too large to bring in." }
              : { name: file.name, database: file };
          }
          // a book among the exports goes on the shelf: people switching apps bring both at once
          const kind = bookFileKind(file);
          if (kind !== "unknown") {
            const unsupported = unsupportedBookMessage(kind);
            return unsupported ? { name: file.name, problem: unsupported } : { name: file.name, book: file };
          }
          if (file.size > MAX_FILE_BYTES) return { name: file.name, problem: "This file is too large to be a reading history." };
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
    const total = totals.books + totals.highlights + databases.length + bookFiles.length;
    const unread: Chosen[] = [];
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
      if (databases.length) {
        const result = await api.importHistoryDatabases(databases);
        sum.books += result.books_added;
        sum.updated += result.books_updated;
        sum.highlights += result.highlights_added;
        sum.skipped += result.skipped;
        done += databases.length;
        setProgress({ done, total });
      }
      let shelved = 0;
      for (const file of bookFiles) {
        try {
          await api.importBook(await prepareBookUpload(file));
          shelved += 1;
        } catch (error) {
          const said = error instanceof Error ? error.message : "";
          if (/already imported/i.test(said)) shelved += 1;
          else unread.push({ name: file.name, problem: said || "We couldn’t add this book." });
        }
        done += 1;
        setProgress({ done, total });
      }
      const free = await onImported();
      setOutcome({ ...sum, shelved, free });
      setChosen(unread);
    } catch (error) {
      // a database the server could not read says why; anything else is a dropped connection
      const said = error instanceof Error && databases.length && /Kobo|Apple Books|database/.test(error.message) ? error.message : null;
      setProblem(said ?? "The import stopped part-way. What was brought in so far is kept; you can run it again safely.");
      await onImported().catch(() => 0);
    } finally {
      setProgress(null);
    }
  };

  const fromReadwise = async () => {
    setAsking(true);
    setProblem(null);
    setOutcome(null);
    try {
      const result = await api.importReadwise(token.trim());
      setToken("");
      const free = await onImported();
      setOutcome({ books: result.books_added, updated: result.books_updated, highlights: result.highlights_added, skipped: result.skipped, free });
    } catch (error) {
      setProblem(error instanceof Error && /Readwise/.test(error.message) ? error.message : "Couldn’t bring your Readwise highlights. Try again.");
    } finally {
      setAsking(false);
    }
  };
  const what = [
    totals.books ? plural(totals.books, "book") : null,
    totals.highlights ? plural(totals.highlights, "highlight") : null,
    databases.length ? plural(databases.length, "reader database") : null,
    bookFiles.length ? `${plural(bookFiles.length, "book")} for your shelf` : null,
  ].filter(Boolean);
  const listed = what.length > 1 ? `${what.slice(0, -1).join(", ")} and ${what[what.length - 1]}` : (what[0] ?? "");

  const source = SOURCES.find((item) => item.id === from) ?? null;

  return (
    <section className="rh-desk" aria-labelledby="rh-desk-title">
      <header className="rh-desk-head">
        <div>
          <h2 id="rh-desk-title">Where have you been reading?</h2>
          <p>Pick one and I’ll show you what to fetch. Already have your files? Drop them below and I’ll work out what each one is.</p>
        </div>
        {onClose && (
          <button className="fl-sheet-close rh-desk-close" aria-label="Close import" onClick={onClose}>
            <Icon name="close" size={18} />
          </button>
        )}
      </header>

      <ul className="rh-sources" aria-label="Reading apps">
        {SOURCES.map((item) => (
          <li key={item.id}>
            <button
              type="button"
              className="rh-source"
              data-tone={item.tone}
              aria-pressed={from === item.id}
              onClick={() => setFrom(from === item.id ? null : item.id)}
            >
              <span className="rh-spine" aria-hidden="true">
                {item.name[0]}
              </span>
              <span className="rh-source-name">{item.name}</span>
              <span className="rh-source-brings">{item.brings}</span>
            </button>
          </li>
        ))}
      </ul>

      {source && (
        <div className="rh-how" role="region" aria-label={`How to bring your ${source.name} history`}>
          {source.ways.map((way, index) => (
            <div className="rh-way" key={way.title ?? index}>
              {way.title && <h3>{way.title}</h3>}
              <ol>
                {way.steps.map((step) => (
                  <li key={step}>{step}</li>
                ))}
              </ol>
            </div>
          ))}
          {source.note && <p className="rh-how-note">{source.note}</p>}
        </div>
      )}

      {from === "readwise" && (
        <form
          className="rh-readwise"
          onSubmit={(event) => {
            event.preventDefault();
            if (token.trim() && !asking) void fromReadwise();
          }}
        >
          <label htmlFor="rh-readwise-token">Your Readwise access token</label>
          <div>
            <input
              id="rh-readwise-token"
              type="password"
              autoComplete="off"
              spellCheck={false}
              placeholder="Paste it here"
              value={token}
              maxLength={120}
              onChange={(event) => setToken(event.currentTarget.value)}
            />
            <Button type="submit" variant="primary" aria-busy={asking} disabled={asking || token.trim().length < 20}>
              {asking ? "Bringing your highlights…" : "Bring from Readwise"}
            </Button>
          </div>
        </form>
      )}

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
        <Moth size={44} mood={progress || asking ? "thinking" : "idle"} />
        <p className="rh-drop-title">
          {source && from !== "readwise" ? `Drop your ${source.name === "Somewhere else" ? "" : `${source.name} `}file here` : "Drop your files here"}
        </p>
        <p className="rh-drop-hint">Exports from any reading app, and any books you have as files. Several at once is fine.</p>
        <Button variant="primary" disabled={progress !== null} onClick={() => input.current?.click()}>
          Choose files
        </Button>
        <input
          ref={input}
          className="sr-only"
          type="file"
          multiple
          accept={`.csv,.txt,.json,.html,.htm,.sqlite,.sqlite3,.db,text/csv,text/plain,application/json,text/html,${BOOK_ACCEPT}`}
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
            <li key={`${file.name}-${index}`} data-bad={!file.parsed && !file.database && !file.book}>
              <strong>{file.name}</strong>
              {file.book ? (
                <span>A book. It goes on your shelf when you import.</span>
              ) : file.database ? (
                <span>A Kobo or Apple Books database. It is read when you import.</span>
              ) : file.parsed ? (
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

      {(ready.length > 0 || databases.length > 0 || bookFiles.length > 0) && (
        <div className="rh-go">
          <Button variant="primary" aria-busy={progress !== null} disabled={progress !== null} onClick={() => void bringIn()}>
            {progress ? `Importing… ${count(progress.done)} of ${count(progress.total)}` : `Import ${listed}`}
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
          <Moth size={40} />
          <span className="m-hand">
            {outcome.shelved ? `Put ${plural(outcome.shelved, "book")} on your shelf. ` : ""}
            {outcome.books || outcome.highlights
              ? `Brought in ${[outcome.books ? plural(outcome.books, "new book") : null, outcome.highlights ? plural(outcome.highlights, "new highlight") : null]
                  .filter(Boolean)
                  .join(" and ")}.`
              : outcome.shelved
                ? ""
                : "Everything in those files was already here."}
            {outcome.updated > 0 && ` Filled in details for ${plural(outcome.updated, "book")} you already had.`}
            {outcome.free > 0 && ` ${plural(outcome.free, "book")} on your list ${outcome.free === 1 ? "is" : "are"} free to read here.`}
            {outcome.skipped > 0 && ` ${plural(outcome.skipped, "item")} could not be kept.`}
          </span>
        </p>
      )}

      <p className="rh-honest">
        This brings your list, ratings, reviews, highlights and notes, and any book files you have without copy
        protection ({BOOK_FORMATS}). Books bought in Kindle, Apple Books and similar stores are usually copy-protected,
        and those files can’t be opened here or anywhere else but their own app. Your files are read by your own Mothli
        and go nowhere else.
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

  const copy = useRef<HTMLInputElement>(null);
  const addOwn = async (file: File | undefined) => {
    if (!file) return;
    const unsupported = unsupportedBookMessage(bookFileKind(file));
    if (unsupported) {
      setProblem(unsupported);
      return;
    }
    setAdding(true);
    setProblem(null);
    try {
      let bookId: string;
      try {
        bookId = (await api.importBook(await prepareBookUpload(file))).book_id;
      } catch (error) {
        // the very file is on the shelf already: it only needs tying to this title
        const already = error instanceof Error ? error.message.match(/already imported as '([^']+)'/) : null;
        if (!already) throw error;
        bookId = already[1];
      }
      await api.setHistoryBook(entry.id, bookId).catch(() => undefined);
      onAdded(entry, bookId);
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
              <>
                <Button variant="primary" aria-busy={adding} disabled={adding} onClick={() => copy.current?.click()}>
                  {adding ? "Adding your copy…" : "Add my copy"}
                </Button>
                <input
                  ref={copy}
                  className="sr-only"
                  type="file"
                  accept={BOOK_ACCEPT}
                  aria-label={`Choose your copy of ${entry.title}`}
                  onChange={(event) => {
                    void addOwn(event.currentTarget.files?.[0]);
                    event.currentTarget.value = "";
                  }}
                />
                <p className="rh-none">
                  We don’t have this book to give you, but you can read your own. Choose the file ({BOOK_FORMATS}),
                  wherever you got it. Only copy-protected files can’t be opened.
                </p>
              </>
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
  const [desk, setDesk] = useState(false);
  const [glimpse, setGlimpse] = useState<{ text: string; entry_id: number; title: string; author: string } | null>(null);

  // something of the reader's own to greet them with; asked for once, and again after an import
  useEffect(() => {
    let stale = false;
    api
      .historyGlimpse()
      .then((found) => {
        if (!stale) setGlimpse(found.mark);
      })
      .catch(() => {});
    return () => {
      stale = true;
    };
  }, [turn]);

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
    // the desk stays open after an import, so what came across is still there to read
    setDesk(true);
    setPage(loaded);
    void api
      .historyGlimpse()
      .then((found) => setGlimpse(found.mark))
      .catch(() => {});
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

  const tally: { n: number; label: string; view: View }[] = page
    ? [
        { n: page.by_status.read, label: page.by_status.read === 1 ? "book read" : "books read", view: "read" as View },
        { n: page.by_status.reading, label: "on the go", view: "reading" as View },
        { n: page.by_status.want, label: "to read", view: "want" as View },
        { n: page.highlights, label: page.highlights === 1 ? "passage marked" : "passages marked", view: "highlights" as View },
        { n: page.free, label: "free to read here", view: "free" as View },
      ].filter((item) => item.n > 0)
    : [];
  const showGlimpse = () => {
    if (!glimpse) return;
    pickedFrom.current = document.activeElement as HTMLElement | null;
    void api
      .historyEntry(glimpse.entry_id)
      .then(setPicked)
      .catch(() => {});
  };

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
        <section className="ml-spread rh-spread" aria-label="Your reading history at a glance">
          <div className="ml-page ml-page--left">
            <p className="m-kicker">From your other reading apps</p>
            <h1 className="ml-spread-title">Your reading history</h1>
            {page && !empty ? (
              <>
                <ul className="rh-tally" aria-label="At a glance">
                  {tally.map((item) => (
                    <li key={item.label}>
                      <button type="button" onClick={() => setView(item.view)} aria-label={`${count(item.n)} ${item.label}`}>
                        <strong>{count(item.n)}</strong>
                        <span>{item.label}</span>
                      </button>
                    </li>
                  ))}
                </ul>
                <div className="ml-spread-foot">
                  <Button variant="primary" className="ml-spread-cta" aria-expanded={desk} onClick={() => setDesk((value) => !value)}>
                    {desk ? "Done importing" : "Bring more in"}
                  </Button>
                </div>
              </>
            ) : (
              <p className="ml-spread-lede">
                Whatever you read, rated and underlined in another app can come with you: your list, your stars, your
                highlights, and any books you have as files.
              </p>
            )}
          </div>
          <div className="ml-page ml-page--right">
            {glimpse && page && !empty ? (
              <figure className="rh-glimpse">
                <figcaption className="ml-note">
                  <Moth size={40} />
                  <span className="m-hand">Something you once underlined</span>
                </figcaption>
                <blockquote>{glimpse.text}</blockquote>
                <button type="button" className="fl-link" onClick={showGlimpse}>
                  {glimpse.title}
                  {glimpse.author ? `, ${glimpse.author}` : ""}
                </button>
              </figure>
            ) : (
              <div className="ml-note ml-note--stacked">
                <Moth size={64} />
                <div className="m-hand ml-note-text ml-note-text--large">
                  {page && !empty ? (
                    <>
                      <p>
                        {plural(page.books, "book")} and {plural(page.highlights, "highlight")} so far.
                      </p>
                      <p>I’ll keep them safe.</p>
                    </>
                  ) : (
                    <>
                      <p>Tell me where you’ve been reading.</p>
                      <p>I’ll show you what to bring, and keep it all safe.</p>
                    </>
                  )}
                </div>
              </div>
            )}
          </div>
        </section>

        {page !== null && (empty || desk) && <Importer onImported={reload} onClose={empty ? undefined : () => setDesk(false)} />}

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

            <ul className="fl-grid rh-grid" aria-label="Books in your history">
              {page.entries.map((entry, index) => {
                // a finished book sits under the year it was finished, like the pages of a reading journal
                const year = view === "read" ? (entry.finished_on?.slice(0, 4) ?? "") : null;
                const before = index > 0 ? (page.entries[index - 1].finished_on?.slice(0, 4) ?? "") : null;
                return (
                  <Fragment key={entry.id}>
                    {year !== null && year !== before && (
                      <li className="rh-year">
                        <h3>{year || "Sometime before"}</h3>
                      </li>
                    )}
                    <li>
                      <button
                        type="button"
                        className="fl-card rh-card"
                        onClick={() => pick(entry)}
                        aria-label={`${entry.title}${entry.author ? `, ${entry.author}` : ""}`}
                      >
                        <Thumb entry={entry} />
                        <span className="fl-card-title">{entry.title}</span>
                        {entry.author && <span className="fl-card-author">{entry.author}</span>}
                        <span className="rh-card-facts">
                          {entry.status && (
                            <span>
                              {STATUS_NAMES[entry.status]}
                              {entry.finished_on ? ` ${longDate(entry.finished_on)}` : ""}
                            </span>
                          )}
                          {entry.rating ? <Stars rating={entry.rating} /> : null}
                          {entry.highlights > 0 && <span>{plural(entry.highlights, "highlight")}</span>}
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
                  </Fragment>
                );
              })}
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
                          setDesk(false);
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
