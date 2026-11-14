/** Reads the files other reading apps let a reader export, in the browser, into plain records:
 * the books they have read or mean to read, and the highlights and notes they made.
 *
 *   Goodreads    goodreads_library_export.csv   (My Books > Tools > Import and export)
 *   StoryGraph   the export CSV                 (Manage Account > Export StoryGraph Library)
 *   Kindle       My Clippings.txt               (the "documents" folder of the device)
 *   Libby        a Reading Journey or Timeline export, as "Data" (JSON)
 *
 * Nothing here opens a book or touches copy protection: these files hold titles, dates and the
 * reader's own words and marked passages. */

export type ImportSource = "goodreads" | "storygraph" | "kindle" | "libby";
export type ReadStatus = "read" | "reading" | "want" | "abandoned";
export type ImportedBook = {
  title: string;
  author: string;
  isbn?: string | null;
  status?: ReadStatus | null;
  rating?: number | null;
  finished_on?: string | null;
  added_on?: string | null;
  review?: string | null;
};
export type ImportedHighlight = {
  title: string;
  author: string;
  text: string;
  note?: string | null;
  location?: string | null;
  position?: number | null;
  made_at?: string | null;
};
export type ParsedHistory = { source: ImportSource; books: ImportedBook[]; highlights: ImportedHighlight[] };

export const SOURCE_NAMES: Record<ImportSource, string> = {
  goodreads: "Goodreads",
  storygraph: "StoryGraph",
  kindle: "Kindle",
  libby: "Libby",
};

/** The file is not one of the exports we read. The message is written for the reader. */
export class UnknownHistoryFile extends Error {}

const clip = (text: string, max: number) => text.trim().slice(0, max);
const blankToNull = (text: string | undefined, max: number) => {
  const value = clip(text ?? "", max);
  return value ? value : null;
};

/** RFC 4180 rows: quoted fields may hold commas, quotes ("") and line breaks. */
export function parseCsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let quoted = false;
  const source = text.charCodeAt(0) === 0xfeff ? text.slice(1) : text;
  for (let i = 0; i < source.length; i += 1) {
    const ch = source[i];
    if (quoted) {
      if (ch === '"' && source[i + 1] === '"') {
        field += '"';
        i += 1;
      } else if (ch === '"') quoted = false;
      else field += ch;
    } else if (ch === '"' && field === "") quoted = true;
    else if (ch === ",") {
      row.push(field);
      field = "";
    } else if (ch === "\n" || ch === "\r") {
      if (ch === "\r" && source[i + 1] === "\n") i += 1;
      row.push(field);
      field = "";
      if (row.length > 1 || row[0] !== "") rows.push(row);
      row = [];
    } else field += ch;
  }
  row.push(field);
  if (row.length > 1 || row[0] !== "") rows.push(row);
  return rows;
}

function table(text: string): Record<string, string>[] {
  const [header, ...rows] = parseCsv(text);
  if (!header) return [];
  const names = header.map((name) => name.trim().toLowerCase());
  return rows.map((row) => Object.fromEntries(names.map((name, index) => [name, row[index] ?? ""])));
}

/** "2021/05/03", "2021-05-03" or "2021/05/01-2021/05/03" (the last day counts) to "2021-05-03". */
function day(text: string | undefined): string | null {
  const found = [...(text ?? "").matchAll(/(\d{4})[/-](\d{1,2})[/-](\d{1,2})/g)].pop();
  if (!found) return null;
  const [, year, month, date] = found;
  const value = `${year}-${month.padStart(2, "0")}-${date.padStart(2, "0")}`;
  return Number.isNaN(Date.parse(value)) ? null : value;
}

function isbn(...candidates: (string | undefined)[]): string | null {
  for (const candidate of candidates) {
    const value = (candidate ?? "").replace(/[^0-9Xx]/g, "");
    if (value.length === 10 || value.length === 13) return value.toUpperCase();
  }
  return null;
}

function stars(text: string | undefined): number | null {
  const value = Number.parseFloat(text ?? "");
  return Number.isFinite(value) && value > 0 && value <= 5 ? value : null;
}

const GOODREADS_SHELVES: Record<string, ReadStatus> = { read: "read", "currently-reading": "reading", "to-read": "want" };
const STORYGRAPH_STATUS: Record<string, ReadStatus> = {
  read: "read",
  "currently-reading": "reading",
  "to-read": "want",
  "did-not-finish": "abandoned",
  paused: "reading",
};

function book(row: Record<string, string>, fields: Partial<ImportedBook>, author: string): ImportedBook | null {
  const title = clip(row.title ?? "", 500);
  if (!title) return null;
  return { title, author: clip(author, 300), ...fields };
}

function goodreads(text: string): ParsedHistory {
  const books = table(text).flatMap((row) => {
    const found = book(
      row,
      {
        isbn: isbn(row.isbn13, row.isbn),
        status: GOODREADS_SHELVES[(row["exclusive shelf"] ?? "").trim().toLowerCase()] ?? null,
        rating: stars(row["my rating"]),
        finished_on: day(row["date read"]),
        added_on: day(row["date added"]),
        review: blankToNull(row["my review"]?.replace(/<br\s*\/?>/gi, "\n"), 20_000),
      },
      row.author ?? "",
    );
    return found ? [found] : [];
  });
  return { source: "goodreads", books, highlights: [] };
}

function storygraph(text: string): ParsedHistory {
  const books = table(text).flatMap((row) => {
    const found = book(
      row,
      {
        isbn: isbn(row["isbn/uid"]),
        status: STORYGRAPH_STATUS[(row["read status"] ?? "").trim().toLowerCase()] ?? null,
        rating: stars(row["star rating"]),
        finished_on: day(row["last date read"]) ?? day(row["dates read"]),
        added_on: day(row["date added"]),
        review: blankToNull(row.review, 20_000),
      },
      row.authors ?? "",
    );
    return found ? [found] : [];
  });
  return { source: "storygraph", books, highlights: [] };
}

const pad = (value: number) => String(value).padStart(2, "0");

/** The device writes local time with no zone; it is kept as written, not shifted to another zone. */
function clippingTime(meta: string): string | null {
  const stamp = meta.split(/\|/).pop()?.replace(/^.*?added on\s+/i, "").trim() ?? "";
  const at = new Date(stamp);
  if (!stamp || Number.isNaN(at.getTime())) return null;
  return `${at.getFullYear()}-${pad(at.getMonth() + 1)}-${pad(at.getDate())}T${pad(at.getHours())}:${pad(at.getMinutes())}:${pad(at.getSeconds())}`;
}

/** The device writes "Austen, Jane" and "Gaiman, Neil;Pratchett, Terry": turned the usual way round. */
function byline(names: string): string {
  return names
    .split(";")
    .map((name) => {
      const parts = name.split(",").map((part) => part.trim());
      return parts.length === 2 && parts[0] && parts[1] ? `${parts[1]} ${parts[0]}` : name.trim();
    })
    .filter(Boolean)
    .join(", ");
}

type Clipping = ImportedHighlight & { start: number | null; end: number | null; isNote: boolean };

function kindle(text: string): ParsedHistory {
  const clippings: Clipping[] = [];
  for (const block of text.replace(/﻿/g, "").split(/^==========\s*$/m)) {
    const lines = block.replace(/\r/g, "").split("\n");
    while (lines.length && !lines[0].trim()) lines.shift();
    if (lines.length < 2) continue;
    const heading = lines[0].trim();
    const meta = lines[1].trim();
    if (!meta.startsWith("-")) continue;
    const body = lines.slice(2).join("\n").trim();
    if (/bookmark/i.test(meta.split("|")[0]) || !body) continue;
    const named = heading.match(/^(.*)\(([^()]*)\)\s*$/);
    const title = clip(named ? named[1] : heading, 500);
    if (!title) continue;
    const place = meta.match(/(?:location|loc\.?)\s+(\d+)(?:\s*-\s*(\d+))?/i) ?? meta.match(/page\s+(\d+)(?:\s*-\s*(\d+))?/i);
    const start = place ? Number(place[1]) : null;
    const isNote = /\bnote\b/i.test(meta.split("|")[0]);
    clippings.push({
      title,
      author: clip(named ? byline(named[2]) : "", 300),
      text: isNote ? "" : clip(body, 8000),
      note: isNote ? clip(body, 8000) : null,
      location: place ? clip(`${/^p/i.test(place[0]) ? "Page" : "Location"} ${place[1]}${place[2] ? `-${place[2]}` : ""}`, 80) : null,
      position: start,
      made_at: clippingTime(meta),
      start,
      end: place?.[2] ? Number(place[2]) : start,
      isNote,
    });
  }
  // Editing a highlight on the device leaves the shorter, earlier version in the file: keep the longest.
  const longest = new Map<string, Clipping>();
  const kept: Clipping[] = [];
  for (const clipping of clippings) {
    if (clipping.isNote || clipping.start === null) {
      kept.push(clipping);
      continue;
    }
    const key = `${clipping.title}\n${clipping.author}\n${clipping.start}`;
    const earlier = longest.get(key);
    if (!earlier) {
      longest.set(key, clipping);
      kept.push(clipping);
    } else if (clipping.text.length > earlier.text.length && clipping.text.startsWith(earlier.text.slice(0, 20))) {
      kept[kept.indexOf(earlier)] = clipping;
      longest.set(key, clipping);
    } else if (!earlier.text.startsWith(clipping.text.slice(0, 20))) kept.push(clipping);
  }
  // A note belongs to the passage it was written on, when that passage is in the file.
  const highlights: ImportedHighlight[] = [];
  const marked = kept.filter((clipping) => !clipping.isNote);
  for (const clipping of kept) {
    if (clipping.isNote) {
      const passage = marked.find(
        (mark) =>
          !mark.note &&
          mark.title === clipping.title &&
          mark.author === clipping.author &&
          mark.start !== null &&
          clipping.start !== null &&
          mark.start <= clipping.start &&
          clipping.start <= (mark.end ?? mark.start),
      );
      if (passage) {
        passage.note = clipping.note;
        continue;
      }
    }
    highlights.push(clipping);
  }
  return {
    source: "kindle",
    books: [],
    highlights: highlights.map(({ title, author, text, note, location, position, made_at }) => ({
      title,
      author,
      text,
      note,
      location,
      position,
      made_at,
    })),
  };
}

type Json = Record<string, unknown>;
const text = (value: unknown): string =>
  typeof value === "string" ? value : value && typeof value === "object" && typeof (value as Json).text === "string" ? ((value as Json).text as string) : "";
const stamp = (value: unknown): string | null => {
  const at = typeof value === "number" ? new Date(value) : null;
  return at && !Number.isNaN(at.getTime()) ? at.toISOString().slice(0, 19) : null;
};

function libby(raw: string): ParsedHistory {
  let data: Json;
  try {
    data = JSON.parse(raw) as Json;
  } catch {
    throw new UnknownHistoryFile("This file isn’t a complete export. Try exporting it again.");
  }
  const journey = data.readingJourney as Json | undefined;
  if (journey && typeof journey === "object") {
    const title = clip(text(journey.title), 500);
    const author = clip(text(journey.author), 300);
    if (!title) throw new UnknownHistoryFile("This Libby export has no book title in it.");
    const marks = Array.isArray(data.highlights) ? (data.highlights as Json[]) : [];
    const highlights = marks.flatMap((mark) => {
      const quote = clip(text(mark.quote), 8000);
      const note = blankToNull(text(mark.note), 8000);
      if (!quote && !note) return [];
      const percent = typeof mark.percent === "number" ? mark.percent : null;
      return [
        {
          title,
          author,
          text: quote,
          note,
          location: blankToNull(text(mark.chapter), 80),
          position: percent,
          made_at: stamp(mark.timestamp),
        },
      ];
    });
    return { source: "libby", books: [{ title, author, isbn: isbn(text(journey.isbn)) }], highlights };
  }
  if (Array.isArray(data.timeline)) {
    // a loan is a book the reader had, not proof they finished it: it is listed without a status
    const seen = new Set<string>();
    const books = (data.timeline as Json[]).flatMap((item) => {
      const title = clip(text(item.title), 500);
      const author = clip(text(item.author), 300);
      const key = `${title}\n${author}`;
      if (!title || seen.has(key)) return [];
      seen.add(key);
      return [{ title, author, isbn: isbn(text(item.isbn)), added_on: stamp(item.timestamp)?.slice(0, 10) ?? null }];
    });
    return { source: "libby", books, highlights: [] };
  }
  throw new UnknownHistoryFile("This isn’t a Libby export we can read. Export a book’s Reading Journey or your Timeline as “Data”.");
}

/** Works out which app a file came from by what is in it, and reads it. */
export function parseHistoryFile(name: string, content: string): ParsedHistory {
  const start = content.replace(/^﻿/, "").trimStart();
  if (start.startsWith("{")) return libby(content);
  if (/^==========\s*$/m.test(content)) return kindle(content);
  const header = (start.split(/\r?\n/, 1)[0] ?? "").toLowerCase();
  if (header.includes("exclusive shelf")) return goodreads(content);
  if (header.includes("read status")) return storygraph(content);
  if (/\.(epub|mobi|azw3?|kfx|pdf|fb2)$/i.test(name)) {
    throw new UnknownHistoryFile("That is a book, not a reading history. Add books from your shelf with “Add a book”.");
  }
  throw new UnknownHistoryFile(
    "We couldn’t tell which app this file is from. We read Goodreads and StoryGraph library exports, Kindle’s “My Clippings.txt” and Libby exports.",
  );
}

export function summarize(files: ParsedHistory[]) {
  return {
    books: files.reduce((sum, file) => sum + file.books.length, 0),
    highlights: files.reduce((sum, file) => sum + file.highlights.length, 0),
    sources: [...new Set(files.map((file) => file.source))],
  };
}
