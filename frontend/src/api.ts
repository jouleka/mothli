/** The backend client (ADR 0007 D-A11 routes). The client NEVER computes the spoiler frontier —
 * it reports (cfi, offset) and renders whatever the clamped server routes return. */

import {
  acknowledgePosition,
  activeOfflineOwner,
  announcePendingDeletion,
  announceSignedOut,
  BOOK_GONE_EVENT,
  applyPendingMarks,
  discardPositionBeforeEpoch,
  forgetBookOnDevice,
  pendingPosition,
  positionRequestBody,
  purgeOfflineTenant,
  queueMarkCreate,
  queueMarkDelete,
  queueMarkUpdate,
  queuePosition,
  trackPositionDelivery,
} from "./pwa/offline";
import { MOTHLI_PLAN, noteMothliPlan, type MothliStanding } from "./ui/mothliStanding";

export type Book = { book_id: string; title: string; author: string | null };
/** A public-domain book from the curated catalogue; `book_id` is set once it is on the shelf. */
export type Classic = { id: string; title: string; author: string; language: string; book_id: string | null };
/** One page of the public-domain catalogue. `total` is its whole size, `matches` this search's. */
export type ClassicsCatalog = {
  enabled: boolean;
  source: string;
  source_url: string;
  total: number;
  matches: number;
  languages: { code: string; count: number }[];
  genres: { id: string; label: string }[];
  moods: { id: string; label: string }[];
  books: Classic[];
  /** `false` where the server has no cover art (hosted mode): the cloth covers are shown. */
  covers?: boolean;
};
/** A title in the reader's history from other reading apps. */
export type HistoryEntry = {
  id: number;
  title: string;
  author: string;
  status: "read" | "reading" | "want" | "abandoned" | null;
  rating: number | null;
  finished_on: string | null;
  added_on: string | null;
  /** The free-library book this title is, when there is one; `book_id` once it is on the shelf. */
  classic_id: string | null;
  book_id: string | null;
  sources: string[];
  highlights: number;
  has_review: boolean;
};
export type HistoryPage = {
  books: number;
  highlights: number;
  with_highlights: number;
  free: number;
  by_status: Record<"read" | "reading" | "want" | "abandoned", number>;
  matches: number;
  entries: HistoryEntry[];
};
export type HistoryDetail = HistoryEntry & {
  review: string | null;
  isbn: string | null;
  marks: { id: number; text: string; note: string | null; location: string | null; made_at: string | null; source: string }[];
};
export type HistoryBatch = { source: string; books: object[]; highlights: object[] };
export type HistoryImported = { books_added: number; books_updated: number; highlights_added: number; skipped: number };
/** What a source read on the server (a Kobo or Apple Books database, Readwise) held. */
export type HistorySources = HistoryImported & { sources: { source: string; books: number; highlights: number }[] };
export type ClassicsSearch = {
  q?: string;
  language?: string;
  genre?: string;
  mood?: string;
  offset?: number;
  limit?: number;
};
/** A row of books on the library's front page: the most read, a mood, or a genre. */
export type ClassicShelf = { kind: "popular" | "mood" | "genre"; id: string; title: string; books: Classic[] };
export type ClassicDetail = Classic & {
  genres: { id: string; label: string }[];
  minutes: number | null;
  blurb: string;
  source_url: string;
};
export type AiRuntime = {
  mode: "demo" | "live";
  provider: string;
  models: { extraction: string; synthesis: string };
  completion_status: "ready" | "unchecked" | "degraded";
  embedding_mode: "demo" | "live";
  local_testing?: boolean;
  reasoning?: { extraction: string; selection: string; synthesis: string; review: string } | null;
};
export type BookType = "novel" | "anthology" | "drama" | "poetry" | "nonfiction" | "reference" | "unknown";
export type BookProfile = {
  book_type: BookType;
  confidence: number;
  detector_version: string;
  signals: string[];
};
export type Atom = { ordinal: number; href: string; title: string; part_label: string; char_len: number; chapter_number?: number | null; content_role?: string | null;
  /** The book's own chapter number (as Mothli counts them); null for a piece in no chapter. */
  content_number?: number | null };
export type Manifest = {
  book_id: string;
  atom_set_version: string;
  mode: string;
  content_language: string;
  book_profile: BookProfile;
  atoms: Atom[];
  content_chapters?: number;
  content_count?: number;
};
export type Position = {
  bookmark: number; cfi: string | null; ingest_progress?: number; position_epoch: number; atoms?: number;
  /** With the read map: the chapter the reader is in now (1-based), 0 when nothing is read. */
  place_chapter?: number;
  completed_chapter?: number; current_offset?: number; high_water_offset?: number;
  position_version?: number; last_opened_at?: string; updated_at?: string; queued?: boolean;
};
export type PutPosition = {
  bookmark: number; current_chapter: number; chapter_progress: number; position_epoch: number;
  cfi?: string | null; completed_chapter?: number; current_offset?: number;
  high_water_offset?: number; position_version?: number; applied?: boolean; conflict?: string | null;
  queued?: boolean;
};
/** A person in the story memory: a number locally, a UUID in hosted mode. */
export type EntityId = number | string;
// a bookmark-bounded surface form -> its entity, so a clicked name opens the one card (LIT-30)
export type CastMember = { name: string; entity_id: EntityId };
export type CatchMeUp = {
  // recap = the flowing "story so far" (hero); now = the tight "right now" one-liner (sidebar);
  // cast = the bookmark-bounded names the recap renders as clickable name affordances (LIT-29/30)
  recap: string | null; now: string | null; as_of_chapter: number;
  cast_size: number; open_threads: number; cast: CastMember[]; cached: boolean;
};
export type ConversationMessage = { role: "user" | "assistant"; content: string; as_of_chapter: number };
/** How much of each chapter the reader has read, 0 to 1. */
export type ReadMap = { chapters: { atom: number; read: number }[] };
/** What another reading app recorded about a book, for the reader to accept or not. */
export type ReadOffer = {
  kind: "finished" | "progress" | "skipped"; sources: string[]; finished_on: string | null;
  chapter: number | null; atoms: number[];
  /** For "skipped": the chapters in words, e.g. "chapters 1 and 2". */
  chapters?: string;
  /** For "skipped": those of them the reader turned through quickly here. */
  skimmed?: number[];
};
/** Chapters before the reader's place not read in full here, and which of them were skimmed. */
export type SkippedChapters = { atoms: number[]; chapters: string; skimmed?: number[] };
/** One message of the home conversation, which runs across all the reader's books. */
export type HomeTurn = { role: "user" | "assistant"; content: string; created_at?: string };
/** One thing Mothli remembers about the reader, from something the reader said. ``book`` is set
 * when it is about one book; ``message`` is the reader's own words, when the server may show them. */
export type Recollection = {
  /** A row number in local mode; in hosted mode a UUID, the only id a hosted URL carries. */
  id: number | string;
  kind: "about" | "taste" | "plan" | "feeling";
  text: string;
  said_at: string;
  book: { book_id: string; title: string } | null;
  message: string | null;
};
export type RecollectionList = { recollections: Recollection[]; hidden: number };
/** One message of the conversation inside a book that the server may show now. `atom` is the
 * chapter the reader was in when it was written. */
export type BookTurn = { role: "user" | "assistant"; content: string; atom: number; reaction?: boolean; created_at?: string };
export type ReadFrontier = { id: string; atom: number; offset: number; position_epoch: number; available_chapter: number };
export type ReadAnchor = { atom: number; quote: string; prefix: string; cfi: string; position_epoch: number };
export type AskClaim = { text: string; citation_ids: number[] };
export type AskCitation = {
  id: number; ordinal: number; chapter_key: string; href: string; title: string; excerpt: string; cfi?: string;
};
export type AskCost = {
  currency: "USD"; usd: string; input_tokens: number; output_tokens: number;
  pricing_known: boolean;
  calls: { provider: string; model: string; usd: string }[];
  payer: string;
};
export const ASK_TIMEOUT_MS = 60_000;
// null until the server has answered once: whether it offers replies sentence by sentence.
let streamedReplies: boolean | null = null;
/** A reply the reader stopped, or one the panel dropped: the request was aborted on purpose. */
export const ASK_STOPPED = "ask_stopped";
/** One controller for a question: aborted when the reply takes too long, or when the caller's
 * ``signal`` fires (the reader pressed Cancel, or the reply was dropped from the screen). Stopping
 * the request matters: a streamed reply the server finishes is stored, and would come back. */
function askController(signal?: AbortSignal) {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), ASK_TIMEOUT_MS);
  const stop = () => controller.abort();
  if (signal?.aborted) controller.abort();
  else signal?.addEventListener("abort", stop, { once: true });
  return {
    signal: controller.signal,
    failed: (error: unknown): unknown =>
      signal?.aborted ? new ApiError("The reply was stopped.", 499, ASK_STOPPED)
      : controller.signal.aborted ? new ApiError("The reply is taking longer than expected.", 504, "ask_timeout")
      : error,
    done: () => {
      window.clearTimeout(timeout);
      signal?.removeEventListener("abort", stop);
    },
  };
}
/** Chapters the reader went past without reading them here; Mothli may offer to count them. */
export type ReadSkipped = { book_id: string; title: string; atoms: number[]; chapters: string };
/** A book from the free library, not on the shelf, that a sentence of Mothli's reply named. */
export type SuggestedBook = { classic_id: string; title: string; author: string };
export type AskAnswer = {
  as_of_chapter: number; insufficient_evidence: boolean; claims: AskClaim[];
  citations: AskCitation[]; cost: AskCost | null;
  /** Present only when the reply said Mothli was not with the reader for some chapters. */
  offers?: ReadSkipped[];
  /** Home conversation only: books the reply suggested that the reader can add to the shelf. */
  suggested?: SuggestedBook[];
  answer_type?: "recap" | "conversation";
  // "prose": the claims are the sentences of one spoken reply, shown as a single paragraph.
  style?: "prose";
  closing?: string;
  calculation?: string;
  message?: string;
  acknowledgment?: string | null;
  recap?: string | null;
  status?: "ready" | "preparing" | "preparation_failed" | "not_started" | "paratext_only" | "ahead";
  read_frontier?: ReadFrontier;
};
export type SelectionAction = "explain" | "define" | "translate";
export type SelectionAssistAnswer = {
  message?: string | null;
  action: SelectionAction; as_of_chapter: number; insufficient_evidence: boolean;
  acknowledgment?: string | null;
  text: string | null; citation: (AskCitation & { cfi: string }) | null; cost: AskCost;
};
export type ChapterCloseout = AskAnswer & { chapter: number };
// a character's tie to another entity, phrased by `direction` relative to the queried character (LIT-30)
export type Tie = { entity_id: EntityId; name: string; rel_type: string; label: string; direction: "in" | "out" };
export type Character = {
  as_of_chapter: number; entity_id: EntityId; name: string; type: string;
  aliases: string[]; first_seen: number; status: unknown; ties: Tie[];
};
export type IngestStatus = { ingest_progress: number; status: string; flags: string[]; error: string | null };
export type GraphNode = {
  entity_id: EntityId;
  canonical_name: string;
  type: string;
  revealed_at: number;
  aliases?: string[];
};
export type GraphEdge = {
  edge_id: EntityId; src_entity: EntityId; dst_entity: EntityId;
  rel_type: string; label: string; revealed_at: number; invalid_at: number | null;
};
export type Graph = { as_of_chapter: number; characters: GraphNode[]; relationships: GraphEdge[] };
// the codex "story broken down" (LIT-31): a chapter + its highlights (who first appears, its events)
export type ChapterNote = {
  chapter_key: string; revealed_at: number; title: string; summary: string;
  new_characters: { entity_id: EntityId; name: string }[]; events: string[];
  /** Hosted only: Mothli could not make notes on this chapter, so its summary is empty. */
  unremembered?: boolean;
};
export type Notes = { as_of_chapter: number; cast: CastMember[]; chapters: ChapterNote[] };
export type ReaderPreferences = {
  font_size: "small" | "book" | "large" | "x-large";
  line_height: "compact" | "comfortable" | "relaxed";
  measure: "narrow" | "balanced" | "wide";
  theme: "paper" | "sepia" | "night" | "system";
  margins: "compact" | "balanced" | "generous";
  typeface: "publisher" | "serif" | "sans";
  preference_version: number;
};
export type BookSearchHit = {
  ordinal: number; href: string; title: string; part_label: string; snippet: string; score: number;
};
export type BookSearch = { as_of_chapter: number; hits: BookSearchHit[] };
export type ReaderMarkAnchor = {
  cfi: string;
  atom: number;
  quote?: { exact: string; prefix: string; suffix: string };
};
type ReaderMarkBase = {
  id: string; anchor: ReaderMarkAnchor; version: number; created_at: string; updated_at: string;
  highlight_id?: string | null;
  pending?: boolean;
};
export type HighlightMark = ReaderMarkBase & {
  kind: "highlight"; color: "yellow" | "green" | "blue" | "pink"; selected_text: string;
  body: null; label: null;
};
export type AnnotationMark = ReaderMarkBase & {
  kind: "annotation"; body: string; color: null; selected_text: null; label: null;
};
export type BookmarkMark = ReaderMarkBase & {
  kind: "bookmark"; label: string | null; color: null; selected_text: null; body: null;
};
export type ReaderMark = HighlightMark | AnnotationMark | BookmarkMark;
export type ReaderMarks = { as_of_chapter: number; marks: ReaderMark[] };
/** Hosted: what Paddle.js needs to show prices and open checkout (public by design), and the
 * price sold for each plan (null for one not on offer). */
export type PlanCheckout = {
  environment: "sandbox" | "production";
  client_token: string;
  prices: { monthly: string | null; yearly: string | null };
};
/** Hosted: where the reader stands with their plan (ADR 0045 section 6). No amount, no count. */
export type Plan = {
  state: "new" | "free" | "ended" | "paying";
  why: "time" | "ceiling" | "plan" | null;
  plan: "monthly" | "yearly" | "gift" | null;
  next_date: string | null;
  /** A live Paddle plan of the reader's own, which they can cancel, keep or manage. */
  subscribed: boolean;
  cancel_scheduled: boolean;
  payment_retrying: boolean;
  /** Whether this server can cancel, keep and manage a plan at all. */
  manage: boolean;
  /** Null where this server opens no checkout. */
  checkout: PlanCheckout | null;
};
/** What every hosted route answers while the reader's account waits to be erased. */
export const PENDING_DELETION = "account_pending_deletion";

export class ApiError extends Error {
  status: number;
  code: string | null;

  constructor(message: string, status: number, code: string | null = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

// Both modes answer a book that is not on this reader's shelf with exactly this.
const UNKNOWN_BOOK = "unknown book";
// FastAPI's own answer for a path this server does not have at all.
const NO_SUCH_ROUTE = "Not Found";

async function j<T>(res: Response): Promise<T> {
  if (!res.ok) {
    // Hosted mode: the sign-in has ended. The app shows the sign-in screen (main.tsx).
    if (res.status === 401) announceSignedOut();
    // surface the server's human message (FastAPI's {"detail": ...}), not raw status + JSON
    const text = await res.text();
    let msg = `the server said no (${res.status})`;
    let code: string | null = null;
    try {
      const detail = (JSON.parse(text) as { detail?: unknown }).detail;
      if (res.status === 404 && detail === UNKNOWN_BOOK) {
        const book = /\/api\/books\/([^/?#]+)/.exec(res.url)?.[1];
        if (book) globalThis.dispatchEvent(new CustomEvent(BOOK_GONE_EVENT, { detail: decodeURIComponent(book) }));
      }
      if (typeof detail === "string" && detail) msg = detail;
      if (detail && typeof detail === "object") {
        const structured = detail as { code?: unknown; message?: unknown; erase_after?: unknown };
        if (typeof structured.message === "string" && structured.message) msg = structured.message;
        if (typeof structured.code === "string" && structured.code) code = structured.code;
        // Hosted mode: the account waits to be erased. The app shows the choice to keep it.
        if (res.status === 403 && code === PENDING_DELETION && typeof structured.erase_after === "string")
          announcePendingDeletion(structured.erase_after);
        // Hosted mode: Mothli needs the plan. Every panel shows its invitation (ADR 0045).
        if (res.status === 402 && code === MOTHLI_PLAN) noteMothliPlan((detail as { why?: unknown }).why);
      }
    } catch {
      if (text) msg = text;
    }
    throw new ApiError(msg, res.status, code);
  }
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

/** A streamed reply's own error event. One refused because Mothli needs the plan (the reader ended
 * between the gate and the reply's first call) is told to the panels, as a 402 answer is. */
type StreamError = { type: "error"; status: number; message: string; code?: string; why?: unknown };
function streamFailure(event: StreamError): ApiError {
  if (event.status === 402 && event.code === MOTHLI_PLAN) noteMothliPlan(event.why);
  return new ApiError(event.message, event.status, event.code ?? null);
}

function csrfHeaders(): Record<string, string> {
  const entry = document.cookie.split(";").map((item) => item.trim()).find(
    (item) => item.startsWith("__Host-litlet-csrf="),
  );
  if (!entry) return {};
  const value = entry.slice(entry.indexOf("=") + 1);
  return value ? { "X-CSRF-Token": decodeURIComponent(value) } : {};
}

/** Routes this server does not have at all, learned from its first "Not Found" this session:
 * hosted mode has none of the reader-key routes, for example. Asked once, then
 * answered here with the same 404, so the console and the server's log are not filled with the
 * same refusal on every page. A 404 that says anything else (a book the reader does not have, a
 * switched-off feature) is never kept: those can change and are asked every time. */
const missingRoutes = new Set<string>();

async function unlessMissing(route: string, request: () => Promise<Response>): Promise<Response> {
  if (missingRoutes.has(route))
    return new Response(JSON.stringify({ detail: NO_SUCH_ROUTE }), {
      status: 404, headers: { "content-type": "application/json" },
    });
  const response = await request();
  if (response.status === 404) {
    const detail = await response.clone().json().then(
      (body: { detail?: unknown } | null) => body?.detail,
      () => null,
    );
    if (detail === NO_SUCH_ROUTE) missingRoutes.add(route);
  }
  return response;
}

/** Tests only: forget what this session learned about the server's routes. */
export function forgetMissingRoutes(): void {
  missingRoutes.clear();
}

function offlineFailure(error: unknown): boolean {
  return error instanceof TypeError || (typeof navigator !== "undefined" && !navigator.onLine);
}

function optimisticBase(anchor: ReaderMarkAnchor) {
  const timestamp = new Date().toISOString();
  return {
    id: `offline-${crypto.randomUUID()}`,
    anchor,
    version: 1,
    created_at: timestamp,
    updated_at: timestamp,
    pending: true,
  };
}

const recapRequests = new Map<string, Promise<CatchMeUp>>();
function requestRecap(id: string, bookmark?: number): Promise<CatchMeUp> {
  const owner = activeOfflineOwner();
  const key = JSON.stringify([owner, id, bookmark ?? null]);
  const pending = recapRequests.get(key);
  if (pending) return pending;
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 60_000);
  const request = unlessMissing("catch-me-up", () =>
    fetch(`/api/books/${id}/catch-me-up${bookmark != null ? `?bookmark=${bookmark}` : ""}`, { signal: controller.signal }))
    .then((response) => j<CatchMeUp>(response))
    .then((value) => {
      if (owner !== activeOfflineOwner()) throw new ApiError("Your reading session changed. Reopen the book.", 409, "session_changed");
      return value;
    })
    .catch((error: unknown) => {
      if (controller.signal.aborted) throw new ApiError("The recap is taking longer than expected.", 504, "recap_timeout");
      throw error;
    })
    .finally(() => {
      window.clearTimeout(timeout);
      if (recapRequests.get(key) === request) recapRequests.delete(key);
    });
  recapRequests.set(key, request);
  return request;
}

export const api = {
  aiRuntime: (signal?: AbortSignal) =>
    fetch("/api/health/ai", { signal, cache: "no-store" }).then((r) => j<AiRuntime>(r)),
  checkAiRuntime: (signal?: AbortSignal) =>
    fetch("/api/health/ai/check", { method: "POST", headers: csrfHeaders(), signal, cache: "no-store" })
      .then((r) => j<AiRuntime>(r)),
  books: () => fetch("/api/books").then((r) => j<Book[]>(r)),
  classics: (search: ClassicsSearch = {}) => {
    const params = new URLSearchParams();
    if (search.q) params.set("q", search.q);
    if (search.language) params.set("language", search.language);
    if (search.genre) params.set("genre", search.genre);
    if (search.mood) params.set("mood", search.mood);
    if (search.offset) params.set("offset", String(search.offset));
    if (search.limit) params.set("limit", String(search.limit));
    const query = params.toString();
    return fetch(`/api/classics${query ? `?${query}` : ""}`).then((r) => j<ClassicsCatalog>(r));
  },
  classicsFront: (language = "") =>
    fetch(`/api/classics/front${language ? `?language=${encodeURIComponent(language)}` : ""}`).then((r) =>
      j<{ shelves: ClassicShelf[]; covers?: boolean }>(r),
    ),
  classic: (id: string) => fetch(`/api/classics/${encodeURIComponent(id)}`).then((r) => j<ClassicDetail>(r)),
  history: (search: { status?: string; only?: string; q?: string; offset?: number; limit?: number } = {}) => {
    const params = new URLSearchParams();
    if (search.status) params.set("status", search.status);
    if (search.only) params.set("only", search.only);
    if (search.q) params.set("q", search.q);
    if (search.offset) params.set("offset", String(search.offset));
    if (search.limit) params.set("limit", String(search.limit));
    const query = params.toString();
    return fetch(`/api/history${query ? `?${query}` : ""}`, { cache: "no-store" }).then((r) => j<HistoryPage>(r));
  },
  /** A passage the reader once marked, picked at random, to greet them with. */
  historyGlimpse: () =>
    fetch("/api/history/glimpse", { cache: "no-store" }).then((r) =>
      j<{ mark: { text: string; entry_id: number; title: string; author: string } | null }>(r),
    ),
  historyEntry: (id: number) => fetch(`/api/history/${id}`, { cache: "no-store" }).then((r) => j<HistoryDetail>(r)),
  importHistory: (batch: HistoryBatch) =>
    fetch("/api/history/import", {
      method: "POST",
      headers: { "content-type": "application/json", ...csrfHeaders() },
      body: JSON.stringify(batch),
    }).then((r) => j<HistoryImported>(r)),
  /** Kobo's KoboReader.sqlite, or the two Apple Books database files from a Mac. */
  importHistoryDatabases: (files: File[]) => {
    const form = new FormData();
    for (const file of files) form.append("files", file, file.name);
    return fetch("/api/history/import-databases", { method: "POST", headers: csrfHeaders(), body: form }).then((r) =>
      j<HistorySources>(r),
    );
  },
  /** Brings a reader's Readwise highlights with the access token they paste; the token is not kept. */
  importReadwise: (token: string) =>
    fetch("/api/history/readwise", {
      method: "POST",
      headers: { "content-type": "application/json", ...csrfHeaders() },
      body: JSON.stringify({ token }),
    }).then((r) => j<HistorySources>(r)),
  /** Records that a book on the shelf is the reader's own copy of a title in their history. */
  setHistoryBook: (id: number, bookId: string) =>
    fetch(`/api/history/${id}/book`, {
      method: "PUT",
      headers: { "content-type": "application/json", ...csrfHeaders() },
      body: JSON.stringify({ book_id: bookId }),
    }).then((r) => j<HistoryDetail>(r)),
  forgetHistory: (id?: number) =>
    fetch(id === undefined ? "/api/history" : `/api/history/${id}`, { method: "DELETE", headers: csrfHeaders() }).then((r) => {
      if (!r.ok) throw new Error("Couldn’t remove it.");
    }),
  /** Says which covers the page is about to show, so the server starts fetching them. */
  warmCovers: (ids: string[]) =>
    fetch("/api/classics/covers/warm", {
      method: "POST",
      headers: { "content-type": "application/json", ...csrfHeaders() },
      body: JSON.stringify({ ids }),
    }).then((r) => j<{ started: number }>(r)),
  addClassic: (id: string) =>
    fetch(`/api/classics/${encodeURIComponent(id)}`, { method: "POST", headers: csrfHeaders() }).then(
      (r) => j<{ book_id: string }>(r),
    ),
  importBook: (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return fetch("/api/books", { method: "POST", headers: csrfHeaders(), body: fd }).then(
      (r) => j<{ book_id: string }>(r),
    );
  },
  /** Takes the book off the reader's shelf, then forgets it on this device (offline copy, what
   * this browser kept for it). A book already gone is gone: the device is cleared all the same. */
  deleteBook: async (id: string) => {
    try {
      await fetch(`/api/books/${encodeURIComponent(id)}`, { method: "DELETE", headers: csrfHeaders() })
        .then((r) => j<void>(r));
    } catch (error) {
      if (!(error instanceof ApiError && error.status === 404)) throw error;
    }
    await forgetBookOnDevice(id);
  },
  manifest: (id: string) => fetch(`/api/books/${id}/manifest`).then((r) => j<Manifest>(r)),
  epubUrl: (id: string) => `/api/books/${id}/epub`,
  position: async (id: string) => {
    const value = await fetch(`/api/books/${id}/position`).then((r) => j<Position>(r));
    const queued = pendingPosition(id);
    if (!queued || (
      queued.position_epoch != null && Number(queued.position_epoch) !== value.position_epoch
    )) return value;
    const completed = Number(queued.completed_chapter ?? value.completed_chapter ?? value.bookmark);
    const offset = Number(queued.offset ?? value.current_offset ?? 0);
    return {
      ...value,
      bookmark: Math.max(value.bookmark, completed),
      completed_chapter: Math.max(value.completed_chapter ?? value.bookmark, completed),
      current_offset: offset,
      high_water_offset: Math.max(value.high_water_offset ?? 0, offset, Number(queued.high_water_offset ?? 0)),
      cfi: typeof queued.cfi === "string" ? queued.cfi : value.cfi,
      queued: true,
    };
  },
  readerPreferences: (id: string) =>
    fetch(`/api/books/${id}/preferences`).then((r) => j<ReaderPreferences>(r)),
  searchBook: (id: string, query: string, limit = 20) => {
    const params = new URLSearchParams({ q: query, limit: String(limit) });
    return fetch(`/api/books/${id}/search?${params}`).then((r) => j<BookSearch>(r));
  },
  readerMarks: async (id: string) => {
    const value = await fetch(`/api/books/${id}/marks`).then((r) => j<ReaderMarks>(r));
    return { ...value, marks: applyPendingMarks(id, value.marks) };
  },
  readerMarksExportUrl: (id: string) => `/api/books/${id}/marks/export`,
  createHighlight: (
    id: string,
    value: { anchor: ReaderMarkAnchor; color: HighlightMark["color"]; selected_text: string },
  ) => fetch(`/api/books/${id}/highlights`, {
    method: "POST",
    headers: { "content-type": "application/json", ...csrfHeaders() },
    body: JSON.stringify(value),
  }).then((r) => j<HighlightMark>(r)).catch((error) => {
    if (!offlineFailure(error)) throw error;
    const mark: HighlightMark = {
      ...optimisticBase(value.anchor),
      kind: "highlight",
      color: value.color,
      selected_text: value.selected_text,
      body: null,
      label: null,
    };
    return queueMarkCreate(id, "highlights", value, mark) as HighlightMark;
  }),
  createAnnotation: (
    id: string,
    value: { anchor: ReaderMarkAnchor; body: string; highlight_id?: string },
  ) => fetch(`/api/books/${id}/annotations`, {
    method: "POST",
    headers: { "content-type": "application/json", ...csrfHeaders() },
    body: JSON.stringify(value),
  }).then((r) => j<AnnotationMark>(r)).catch((error) => {
    if (!offlineFailure(error)) throw error;
    const mark: AnnotationMark = {
      ...optimisticBase(value.anchor),
      kind: "annotation",
      body: value.body,
      highlight_id: value.highlight_id ?? null,
      color: null,
      selected_text: null,
      label: null,
    };
    return queueMarkCreate(id, "annotations", value, mark) as AnnotationMark;
  }),
  createBookmark: (id: string, value: { anchor: ReaderMarkAnchor; label?: string }) =>
    fetch(`/api/books/${id}/bookmarks`, {
      method: "POST",
      headers: { "content-type": "application/json", ...csrfHeaders() },
      body: JSON.stringify(value),
    }).then((r) => j<BookmarkMark>(r)).catch((error) => {
      if (!offlineFailure(error)) throw error;
      const mark: BookmarkMark = {
        ...optimisticBase(value.anchor),
        kind: "bookmark",
        label: value.label ?? null,
        color: null,
        selected_text: null,
        body: null,
      };
      return queueMarkCreate(id, "bookmarks", value, mark) as BookmarkMark;
    }),
  updateReaderMark: (id: string, mark: ReaderMark, value: string) => {
    const collection = `${mark.kind}s`;
    const key = mark.kind === "highlight" ? "color" : mark.kind === "annotation" ? "body" : "label";
    const body = { [key]: value };
    return fetch(`/api/books/${id}/${collection}/${mark.id}`, {
      method: "PATCH",
      headers: { "content-type": "application/json", ...csrfHeaders() },
      body: JSON.stringify(body),
    }).then((r) => j<ReaderMark>(r)).catch((error) => {
      if (!offlineFailure(error)) throw error;
      return queueMarkUpdate(id, mark, body);
    });
  },
  deleteReaderMark: (id: string, mark: ReaderMark) =>
    fetch(`/api/books/${id}/${mark.kind}s/${mark.id}`, {
      method: "DELETE",
      headers: csrfHeaders(),
    }).then((r) => j<void>(r)).catch((error) => {
      if (!offlineFailure(error)) throw error;
      queueMarkDelete(id, mark);
    }),
  putReaderPreferences: (id: string, preferences: Omit<ReaderPreferences, "preference_version">) =>
    fetch(`/api/books/${id}/preferences`, {
      method: "PUT",
      headers: { "content-type": "application/json", ...csrfHeaders() },
      body: JSON.stringify(preferences),
      keepalive: true,
    }).then((r) => j<ReaderPreferences>(r)),
  putPosition: (
    id: string,
    cfi: string,
    offset: number,
    completedChapter: number,
    positionEpoch: number,
    baseVersion: number,
    clientId: string,
    clientSequence: number,
  ) => {
    const owner = activeOfflineOwner();
    const url = `/api/books/${id}/position`;
    const body = {
      cfi,
      offset,
      completed_chapter: completedChapter,
      position_epoch: positionEpoch,
      base_version: baseVersion,
      client_id: clientId,
      client_sequence: clientSequence,
    };
    // keepalive: the flush-on-pagehide report must survive the page teardown
    let requestBody: Record<string, unknown> = body;
    return trackPositionDelivery(id, owner, () => {
      if (activeOfflineOwner() !== owner)
        return Promise.reject(new ApiError("the reader session changed before the position could be saved", 409));
      requestBody = positionRequestBody(id, owner, body);
      return fetch(url, {
        method: "PUT",
        headers: { "content-type": "application/json", ...csrfHeaders() },
        body: JSON.stringify(requestBody),
        keepalive: true,
      }).then((r) => j<PutPosition>(r)).then((saved) => {
        acknowledgePosition(id, requestBody, saved, owner);
        return saved;
      }).catch((error) => {
        // Signed out mid-read (hosted): keep the place like an offline one, sent after signing in.
        if (!offlineFailure(error) && !(error instanceof ApiError && error.status === 401)) throw error;
        if (activeOfflineOwner() !== owner)
          throw new ApiError("the reader session changed before the position could be saved", 409);
        return queuePosition(id, url, requestBody) as PutPosition;
      });
    });
  },
  resetPosition: (id: string, positionEpoch: number, fresh = false) => {
    const owner = activeOfflineOwner();
    return fetch(`/api/books/${id}/position/reset`, {
      method: "POST",
      headers: { "content-type": "application/json", ...csrfHeaders() },
      body: JSON.stringify({ position_epoch: positionEpoch, ...(fresh ? { fresh: true } : {}) }),
    }).then((r) => j<Position>(r)).then((saved) => {
      discardPositionBeforeEpoch(id, saved.position_epoch, owner);
      return saved;
    });
  },
  // bookmark is optional: omitted = the current frontier; set = the scrubber's past point (LIT-15).
  // Either way the server CLAMPS to the high-water — the client never widens its own frontier.
  catchMeUp: requestRecap,
  readFrontier: (id: string, anchor?: ReadAnchor) => fetch(`/api/books/${id}/read-frontier`, anchor ? {
    method: "PUT", headers: { "content-type": "application/json", ...csrfHeaders() }, body: JSON.stringify(anchor),
  } : {}).then((r) => j<ReadFrontier>(r)),
  askBook: (id: string, question: string, bookmark?: number, history?: ConversationMessage[], readFrontier?: ReadFrontier, signal?: AbortSignal) => {
    // A reply that has not arrived in a minute is not coming: stop waiting instead of leaving
    // the reader on the thinking dots with no way forward but Cancel.
    const request = askController(signal);
    return fetch(`/api/books/${id}/ask`, {
      method: "POST",
      headers: { "content-type": "application/json", ...csrfHeaders() },
      body: JSON.stringify({ question, ...(bookmark != null ? { bookmark } : {}), ...(history?.length ? { history } : {}), ...(readFrontier ? {read_frontier:readFrontier} : {}) }),
      signal: request.signal,
    }).then((r) => j<AskAnswer>(r)).catch((error: unknown) => {
      throw request.failed(error);
    }).finally(request.done);
  },
  /**
   * A reply delivered sentence by sentence, each already checked by the server. Resolves with
   * the complete answer, or with null when this server does not offer streamed replies (the
   * caller then asks the ordinary way).
   */
  /** The read map: what the reader has read of this book. 404 while the map is off. */
  readMap: (id: string) => fetch(`/api/books/${id}/read-map`).then((r) => j<ReadMap>(r)),
  /** A page read, or with ``skimmed`` one turned quickly on the way (remembered, never read). */
  readPage: (id: string, start: ReadAnchor, end: ReadAnchor, skimmed = false) => fetch(`/api/books/${id}/read-page`, {
    method: "PUT", headers: { "content-type": "application/json", ...csrfHeaders() },
    body: JSON.stringify(skimmed ? { start, end, skimmed } : { start, end }),
  }).then((r) => j<ReadFrontier & { skipped?: SkippedChapters | null }>(r)),
  readChapters: (id: string, atoms: number[], read: boolean, source: "told" | "import" = "told") =>
    fetch(`/api/books/${id}/read-chapters`, {
      method: "PUT", headers: { "content-type": "application/json", ...csrfHeaders() },
      body: JSON.stringify({ atoms, read, ...(source === "import" ? { source } : {}) }),
    }).then((r) => j<ReadFrontier>(r)),
  readOffer: (id: string) =>
    unlessMissing("read-offer", () => fetch(`/api/books/${id}/read-offer`)).then((r) => j<{ offer: ReadOffer | null }>(r)),
  /** The home conversation. 404 where the server does not have it switched on. */
  homeConversation: () => fetch("/api/reader/conversation").then((r) => j<{ turns: HomeTurn[] }>(r)),
  clearHome: () =>
    fetch("/api/reader/conversation", { method: "DELETE", headers: csrfHeaders() }).then(() => undefined),
  /** What Mothli remembers about the reader, newest first, and how many more are held back until
   * their pages are read again. 404 where the server does not keep any. */
  recollections: () =>
    fetch("/api/reader/recollections", { cache: "no-store" }).then((r) => j<RecollectionList>(r)),
  forgetRecollection: (id: Recollection["id"]) =>
    fetch(`/api/reader/recollections/${encodeURIComponent(String(id))}`, { method: "DELETE", headers: csrfHeaders() }).then((r) => {
      if (!r.ok && r.status !== 404) throw new ApiError("Couldn’t forget that.", r.status);
    }),
  forgetRecollections: () =>
    fetch("/api/reader/recollections", { method: "DELETE", headers: csrfHeaders() }).then((r) => {
      if (!r.ok) throw new ApiError("Couldn’t forget them.", r.status);
    }),
  askHome: async (question: string, onSentence: (text: string) => void, signal?: AbortSignal): Promise<AskAnswer> => {
    const request = askController(signal);
    try {
      const response = await fetch("/api/reader/ask/stream", {
        method: "POST",
        headers: { "content-type": "application/json", ...csrfHeaders() },
        body: JSON.stringify({ question }),
        signal: request.signal,
      });
      if (!response.ok || !response.body) return await j<AskAnswer>(response);
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        for (let end = buffer.indexOf("\n\n"); end >= 0; end = buffer.indexOf("\n\n")) {
          const line = buffer.slice(0, end);
          buffer = buffer.slice(end + 2);
          if (!line.startsWith("data: ")) continue;
          const event = JSON.parse(line.slice(6)) as
            | { type: "sentence"; text: string }
            | { type: "done"; answer: AskAnswer }
            | StreamError;
          if (event.type === "sentence") onSentence(event.text);
          else if (event.type === "done") return event.answer;
          else throw streamFailure(event);
        }
      }
      throw new ApiError("The reply ended early.", 502);
    } catch (error) {
      throw request.failed(error);
    } finally {
      request.done();
    }
  },
  /** The conversation inside a book, as the server may show it now: only turns written from
   * text that is still read. The chat panel shows this and keeps no copy of its own. */
  bookConversation: (id: string) =>
    fetch(`/api/books/${id}/conversation`).then((r) => j<{ read_frontier?: ReadFrontier; turns: BookTurn[] }>(r)),
  clearConversation: (id: string) =>
    fetch(`/api/books/${id}/conversation`, { method: "DELETE", headers: csrfHeaders() }).then((r) => {
      if (!r.ok) throw new ApiError("The conversation could not be cleared.", r.status);
    }),
  // No earlier conversation is sent: the server keeps its own record and reads nothing else.
  askBookStream: async (
    id: string, question: string, bookmark: number,
    readFrontier: ReadFrontier, onSentence: (text: string) => void, signal?: AbortSignal,
  ): Promise<AskAnswer | null> => {
    if (streamedReplies === false) return null;
    // Aborting stops the server too: it stores a turn only once the reply is done.
    const request = askController(signal);
    try {
      const response = await fetch(`/api/books/${id}/ask/stream`, {
        method: "POST",
        headers: { "content-type": "application/json", ...csrfHeaders() },
        body: JSON.stringify({ question, bookmark, read_frontier: readFrontier }),
        signal: request.signal,
      });
      if (response.status === 404 || response.status === 405) {
        if (response.status === 405 || /not enabled|not found/i.test(await response.clone().text())) {
          streamedReplies = false;
          return null;
        }
      }
      if (!response.ok || !response.body) return await j<AskAnswer>(response);
      streamedReplies = true;
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        for (let end = buffer.indexOf("\n\n"); end >= 0; end = buffer.indexOf("\n\n")) {
          const line = buffer.slice(0, end);
          buffer = buffer.slice(end + 2);
          if (!line.startsWith("data: ")) continue;
          const event = JSON.parse(line.slice(6)) as
            | { type: "sentence"; text: string }
            | { type: "done"; answer: AskAnswer }
            | StreamError;
          if (event.type === "sentence") onSentence(event.text);
          else if (event.type === "done") return event.answer;
          else throw streamFailure(event);
        }
      }
      throw new ApiError("The reply ended early.", 502);
    } catch (error) {
      throw request.failed(error);
    } finally {
      request.done();
    }
  },
  // Help with a picked passage and the chapter look-back: local mode only. Hosted mode has neither
  // route (ADR 0043), and its screens never ask (ReaderPage's ``passageHelp`` and "Tell me").
  selectionAction: (
    id: string,
    value: { action: SelectionAction; text: string; atom: number; cfi: string; question?: string; history?: ConversationMessage[] },
  ) => {
    if (Array.from(value.text).length > 2000)
      return Promise.reject(new ApiError("Select a shorter passage (up to 2,000 characters).",413,"selection_too_large"));
    return unlessMissing("selection-action", () => fetch(`/api/books/${id}/selection-action`, {
    method: "POST",
    headers: { "content-type": "application/json", ...csrfHeaders() },
    body: JSON.stringify({
      ...value,
      ...(value.action === "translate" ? { target_language: "English" } : {}),
    }),
  })).then((r) => j<SelectionAssistAnswer>(r));
  },
  chapterCloseout: (id: string, chapter: number) =>
    unlessMissing("chapter-closeout", () => fetch(`/api/books/${id}/chapter-closeout`, {
      method: "POST",
      headers: { "content-type": "application/json", ...csrfHeaders() },
      body: JSON.stringify({ chapter }),
    })).then((r) => j<ChapterCloseout>(r)),
  // a character's bookmark-clamped card (identity + ties); server 404s a future/unknown entity (LIT-30)
  character: (id: string, entityId: EntityId, bookmark?: number) =>
    unlessMissing("character", () => fetch(
      `/api/books/${id}/character/${encodeURIComponent(entityId)}${bookmark != null ? `?bookmark=${bookmark}` : ""}`,
    )).then((r) => j<Character>(r)),
  ingest: (id: string) => unlessMissing("ingest", () => fetch(`/api/books/${id}/ingest`)).then((r) => j<IngestStatus>(r)),
  retryIngest: (id: string) => fetch(`/api/books/${id}/ingest/retry`, { method: "POST", headers: csrfHeaders() })
    .then((r) => j<{status: string}>(r)),
  // the character graph as of `bookmark` (the scrubber's point); server clamps to the high-water
  graph: (id: string, bookmark?: number) =>
    unlessMissing("graph", () => fetch(`/api/books/${id}/graph${bookmark != null ? `?bookmark=${bookmark}` : ""}`)).then(
      (r) => j<Graph>(r),
    ),
  visiblePeople: (id: string, point: ReadFrontier) => unlessMissing("people", () => fetch(`/api/books/${id}/people`,{
    method:"POST",headers:{"content-type":"application/json",...csrfHeaders()},body:JSON.stringify(point),
  })).then((r)=>j<{read_frontier:ReadFrontier;names:string[]}>(r)),
  // the chapter-by-chapter breakdown (summary + highlights) + visible cast, clamped to `bookmark` (LIT-31)
  notes: (id: string, bookmark?: number) =>
    unlessMissing("notes", () => fetch(`/api/books/${id}/notes${bookmark != null ? `?bookmark=${bookmark}` : ""}`)).then(
      (r) => j<Notes>(r),
    ),
  logout: async () => {
    try {
      await fetch("/api/auth/logout", { method: "POST", headers: csrfHeaders() }).then(
        (r) => j<void>(r),
      );
    } finally {
      await purgeOfflineTenant();
    }
  },
  /** Hosted: ask for the account to be erased after 30 days. Signs out on every device at once;
   * the books saved on this one for offline reading go too. Answers the moment of erasure. */
  deleteAccount: async () => {
    const value = await fetch("/api/account/delete", { method: "POST", headers: csrfHeaders() }).then(
      (r) => j<{ erase_after: string }>(r),
    );
    await purgeOfflineTenant();
    return value;
  },
  /** Hosted: undo a pending deletion; an ordinary account again, everything as it was. */
  keepAccount: () =>
    fetch("/api/account/keep", { method: "POST", headers: csrfHeaders() }).then((r) => j<void>(r)),
  /** Hosted: the reader opened the cast or "Story so far" themselves, their first use of Mothli:
   * the server starts their free weeks once and answers where they stand. Never called by the
   * app's own fetches. */
  startMothli: (bookId?: string) =>
    fetch("/api/reader/mothli/start", {
      method: "POST",
      headers: { "content-type": "application/json", ...csrfHeaders() },
      body: JSON.stringify(bookId ? { book_id: bookId } : {}),
    }).then((r) => j<MothliStanding>(r)),
  /** Hosted: the reader's plan (ADR 0045). */
  plan: () => fetch("/api/billing/plan", { cache: "no-store" }).then((r) => j<Plan>(r)),
  /** Hosted, signed in or not: Paddle's environment and public client token, for the payment-link
   * page (`/?_ptxn=…`), and nothing else. */
  paymentLink: () =>
    fetch("/api/billing/payment-link", { cache: "no-store" })
      .then((r) => j<Pick<PlanCheckout, "environment" | "client_token">>(r)),
  /** Hosted: a checkout the server made for this reader, for Paddle.js to open by its id. */
  startCheckout: (plan: "monthly" | "yearly") =>
    fetch("/api/billing/checkout", {
      method: "POST",
      headers: { "content-type": "application/json", ...csrfHeaders() },
      body: JSON.stringify({ plan }),
    }).then((r) => j<{ transaction_id: string }>(r)),
  /** Hosted: cancel at the end of the period paid for; answers the plan. */
  cancelPlan: () =>
    fetch("/api/billing/cancel", { method: "POST", headers: csrfHeaders() }).then((r) => j<Plan>(r)),
  /** Hosted: undo a scheduled cancel; answers the plan. */
  keepPlan: () =>
    fetch("/api/billing/keep", { method: "POST", headers: csrfHeaders() }).then((r) => j<Plan>(r)),
  /** Hosted: a one-time link to Paddle's customer portal. Never kept. */
  planPortal: () =>
    fetch("/api/billing/portal", { method: "POST", headers: csrfHeaders() }).then((r) => j<{ url: string }>(r)),
  /** Hosted: the reader's copy of their data, one zip, with the name the server gives it. Open
   * to a reader waiting to be erased too. */
  exportAccount: async (): Promise<{ blob: Blob; name: string }> => {
    const response = await fetch("/api/account/export", { cache: "no-store" });
    if (!response.ok) await j<never>(response);
    const name = /filename="([^"]+)"/.exec(response.headers.get("content-disposition") ?? "")?.[1];
    return { blob: await response.blob(), name: name ?? "mothli-my-data.zip" };
  },
};
