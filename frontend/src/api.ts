/** The backend client (ADR 0007 D-A11 routes). The client NEVER computes the spoiler frontier —
 * it reports (cfi, offset) and renders whatever the clamped server routes return. */

import {
  acknowledgePosition,
  activeOfflineOwner,
  applyPendingMarks,
  discardPositionBeforeEpoch,
  pendingPosition,
  positionRequestBody,
  purgeOfflineTenant,
  queueMarkCreate,
  queueMarkDelete,
  queueMarkUpdate,
  queuePosition,
  trackPositionDelivery,
} from "./pwa/offline";

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
export type Atom = { ordinal: number; href: string; title: string; part_label: string; char_len: number; chapter_number?: number | null; content_role?: string | null };
export type Manifest = {
  book_id: string;
  atom_set_version: string;
  mode: string;
  content_language: string;
  book_profile: BookProfile;
  atoms: Atom[];
  content_chapters?: number;
};
export type Position = {
  bookmark: number; cfi: string | null; ingest_progress?: number; position_epoch: number; atoms?: number;
  completed_chapter?: number; current_offset?: number; high_water_offset?: number;
  position_version?: number; last_opened_at?: string; updated_at?: string; queued?: boolean;
};
export type PutPosition = {
  bookmark: number; current_chapter: number; chapter_progress: number; position_epoch: number;
  cfi?: string | null; completed_chapter?: number; current_offset?: number;
  high_water_offset?: number; position_version?: number; applied?: boolean; conflict?: string | null;
  queued?: boolean;
};
// a bookmark-bounded surface form -> its entity, so a clicked name opens the one card (LIT-30)
export type CastMember = { name: string; entity_id: number };
export type CatchMeUp = {
  // recap = the flowing "story so far" (hero); now = the tight "right now" one-liner (sidebar);
  // cast = the bookmark-bounded names the recap renders as clickable name affordances (LIT-29/30)
  recap: string | null; now: string | null; as_of_chapter: number;
  cast_size: number; open_threads: number; cast: CastMember[]; cached: boolean;
};
export type ConversationMessage = { role: "user" | "assistant"; content: string; as_of_chapter: number };
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
export type AskAnswer = {
  as_of_chapter: number; insufficient_evidence: boolean; claims: AskClaim[];
  citations: AskCitation[]; cost: AskCost | null;
  answer_type?: "recap" | "conversation";
  calculation?: string;
  message?: string;
  acknowledgment?: string | null;
  recap?: string | null;
  status?: "ready" | "preparing" | "preparation_failed" | "not_started" | "paratext_only";
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
export type Tie = { entity_id: number; name: string; rel_type: string; label: string; direction: "in" | "out" };
export type Character = {
  as_of_chapter: number; entity_id: number; name: string; type: string;
  aliases: string[]; first_seen: number; status: unknown; ties: Tie[];
};
export type IngestStatus = { ingest_progress: number; status: string; flags: string[]; error: string | null };
export type GraphNode = {
  entity_id: number;
  canonical_name: string;
  type: string;
  revealed_at: number;
  aliases?: string[];
};
export type GraphEdge = {
  edge_id: number; src_entity: number; dst_entity: number;
  rel_type: string; label: string; revealed_at: number; invalid_at: number | null;
};
export type Graph = { as_of_chapter: number; characters: GraphNode[]; relationships: GraphEdge[] };
export type MemoryCorrection = {
  correction_id: number | string;
  kind: "replace" | "split" | "merge";
  effective_at: number;
  source_entities: { entity_id: number | string; name: string }[];
  target_entities: { entity_id: number | string; name: string }[];
  reason: string | null;
  recorded_at: string;
};
export type MemoryCorrections = { as_of_chapter: number; items: MemoryCorrection[] };
// the codex "story broken down" (LIT-31): a chapter + its highlights (who first appears, its events)
export type ChapterNote = {
  chapter_key: string; revealed_at: number; title: string; summary: string;
  new_characters: { entity_id: number; name: string }[]; events: string[];
};
export type Notes = { as_of_chapter: number; cast: CastMember[]; chapters: ChapterNote[] };
export type ProviderCapability = "extraction" | "synthesis" | "embedding" | "judge";
export type HostedProvider = "openai-compatible" | "anthropic" | "offline";
export type CredentialMetadata = {
  id: string; provider: Exclude<HostedProvider, "offline">; masked_label: string; key_version: string;
  created_at: string; rotated_at: string | null; disabled_at: string | null;
};
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
export type ProviderSetting = {
  id: string; provider: HostedProvider; capability: ProviderCapability; credential_id: string | null;
  model: string; base_url: string | null; enabled: boolean;
  validation_status: "unchecked" | "ready" | "offline" | "invalid";
  validation_error_code: string | null; validated_at: string | null;
  created_at: string; updated_at: string;
};
export type ProviderRecommendation = { provider: HostedProvider; model: string; base_url: string | null };
export type ProviderSettingsPayload = {
  capabilities: ProviderCapability[]; providers: HostedProvider[];
  recommendations: Record<ProviderCapability, ProviderRecommendation>;
  recommendations_persisted: false; offline_behavior: string; cost_ownership: string;
  items: ProviderSetting[];
};
export type ProviderSettingInput = {
  provider: HostedProvider; model: string; credential_id: string | null; base_url?: string | null;
};
export type ProviderValidation = {
  status: "ready" | "offline" | "invalid";
  code: "ok" | "offline" | "invalid_credentials" | "unavailable_model" | "network_error" | "service_error";
  setting: ProviderSetting;
};

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

async function j<T>(res: Response): Promise<T> {
  if (!res.ok) {
    // surface the server's human message (FastAPI's {"detail": ...}), not raw status + JSON
    const text = await res.text();
    let msg = `the server said no (${res.status})`;
    let code: string | null = null;
    try {
      const detail = (JSON.parse(text) as { detail?: unknown }).detail;
      if (typeof detail === "string" && detail) msg = detail;
      if (detail && typeof detail === "object") {
        const structured = detail as { code?: unknown; message?: unknown };
        if (typeof structured.message === "string" && structured.message) msg = structured.message;
        if (typeof structured.code === "string" && structured.code) code = structured.code;
      }
    } catch {
      if (text) msg = text;
    }
    throw new ApiError(msg, res.status, code);
  }
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

function csrfHeaders(): Record<string, string> {
  const entry = document.cookie.split(";").map((item) => item.trim()).find(
    (item) => item.startsWith("__Host-litlet-csrf="),
  );
  if (!entry) return {};
  const value = entry.slice(entry.indexOf("=") + 1);
  return value ? { "X-CSRF-Token": decodeURIComponent(value) } : {};
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
  const request = fetch(`/api/books/${id}/catch-me-up${bookmark != null ? `?bookmark=${bookmark}` : ""}`, { signal: controller.signal })
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
      j<{ shelves: ClassicShelf[] }>(r),
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
        if (!offlineFailure(error)) throw error;
        if (activeOfflineOwner() !== owner)
          throw new ApiError("the reader session changed before the position could be saved", 409);
        return queuePosition(id, url, requestBody) as PutPosition;
      });
    });
  },
  resetPosition: (id: string, positionEpoch: number) => {
    const owner = activeOfflineOwner();
    return fetch(`/api/books/${id}/position/reset`, {
      method: "POST",
      headers: { "content-type": "application/json", ...csrfHeaders() },
      body: JSON.stringify({ position_epoch: positionEpoch }),
    }).then((r) => j<Position>(r)).then((saved) => {
      discardPositionBeforeEpoch(id, saved.position_epoch, owner);
      return saved;
    });
  },
  // bookmark is optional: omitted = the current frontier; set = the scrubber's past point (LIT-15).
  // Either way the server CLAMPS to the high-water — the client never widens its own frontier.
  catchMeUp: requestRecap,
  preparedQuestions: (id: string, bookmark: number) =>
    fetch(`/api/books/${id}/prepared-questions?bookmark=${bookmark}`).then(
      (r) => j<{ as_of_chapter: number; questions: string[] }>(r),
    ),
  readFrontier: (id: string, anchor?: ReadAnchor) => fetch(`/api/books/${id}/read-frontier`, anchor ? {
    method: "PUT", headers: { "content-type": "application/json", ...csrfHeaders() }, body: JSON.stringify(anchor),
  } : {}).then((r) => j<ReadFrontier>(r)),
  askBook: (id: string, question: string, bookmark?: number, history?: ConversationMessage[], readFrontier?: ReadFrontier) =>
    fetch(`/api/books/${id}/ask`, {
      method: "POST",
      headers: { "content-type": "application/json", ...csrfHeaders() },
      body: JSON.stringify({ question, ...(bookmark != null ? { bookmark } : {}), ...(history?.length ? { history } : {}), ...(readFrontier ? {read_frontier:readFrontier} : {}) }),
    }).then((r) => j<AskAnswer>(r)),
  selectionAction: (
    id: string,
    value: { action: SelectionAction; text: string; atom: number; cfi: string; question?: string; history?: ConversationMessage[] },
  ) => {
    if (Array.from(value.text).length > 2000)
      return Promise.reject(new ApiError("Select a shorter passage (up to 2,000 characters).",413,"selection_too_large"));
    return fetch(`/api/books/${id}/selection-action`, {
    method: "POST",
    headers: { "content-type": "application/json", ...csrfHeaders() },
    body: JSON.stringify({
      ...value,
      ...(value.action === "translate" ? { target_language: "English" } : {}),
    }),
  }).then((r) => j<SelectionAssistAnswer>(r));
  },
  chapterCloseout: (id: string, chapter: number) =>
    fetch(`/api/books/${id}/chapter-closeout`, {
      method: "POST",
      headers: { "content-type": "application/json", ...csrfHeaders() },
      body: JSON.stringify({ chapter }),
    }).then((r) => j<ChapterCloseout>(r)),
  // a character's bookmark-clamped card (identity + ties); server 404s a future/unknown entity (LIT-30)
  character: (id: string, entityId: number, bookmark?: number) =>
    fetch(
      `/api/books/${id}/character/${entityId}${bookmark != null ? `?bookmark=${bookmark}` : ""}`,
    ).then((r) => j<Character>(r)),
  ingest: (id: string) => fetch(`/api/books/${id}/ingest`).then((r) => j<IngestStatus>(r)),
  retryIngest: (id: string) => fetch(`/api/books/${id}/ingest/retry`, { method: "POST", headers: csrfHeaders() })
    .then((r) => j<{status: string}>(r)),
  // the character graph as of `bookmark` (the scrubber's point); server clamps to the high-water
  graph: (id: string, bookmark?: number) =>
    fetch(`/api/books/${id}/graph${bookmark != null ? `?bookmark=${bookmark}` : ""}`).then(
      (r) => j<Graph>(r),
    ),
  visiblePeople: (id: string, point: ReadFrontier) => fetch(`/api/books/${id}/people`,{
    method:"POST",headers:{"content-type":"application/json",...csrfHeaders()},body:JSON.stringify(point),
  }).then((r)=>j<{read_frontier:ReadFrontier;names:string[]}>(r)),
  memoryCorrections: (id: string, bookmark?: number) =>
    fetch(
      `/api/books/${id}/memory-corrections${bookmark != null ? `?bookmark=${bookmark}` : ""}`,
    ).then((r) => j<MemoryCorrections>(r)),
  correctMemory: (
    id: string,
    value: { source_entity_id: number; canonical_name: string; reason: string; bookmark: number },
  ) => fetch(`/api/books/${id}/memory-corrections`, {
    method: "POST",
    headers: { "content-type": "application/json", ...csrfHeaders() },
    body: JSON.stringify(value),
  }).then((r) => j<MemoryCorrections & { correction_id: number | string; target_entity_id: number | string }>(r)),
  // the chapter-by-chapter breakdown (summary + highlights) + visible cast, clamped to `bookmark` (LIT-31)
  notes: (id: string, bookmark?: number) =>
    fetch(`/api/books/${id}/notes${bookmark != null ? `?bookmark=${bookmark}` : ""}`).then(
      (r) => j<Notes>(r),
    ),
  credentials: () => fetch("/api/credentials").then((r) => j<CredentialMetadata[]>(r)),
  createCredential: (provider: Exclude<HostedProvider, "offline">, secret: string) =>
    fetch("/api/credentials", {
      method: "POST",
      headers: { "content-type": "application/json", ...csrfHeaders() },
      body: JSON.stringify({ provider, secret }),
    }).then((r) => j<CredentialMetadata>(r)),
  deleteCredential: (id: string) =>
    fetch(`/api/credentials/${id}`, { method: "DELETE", headers: csrfHeaders() }).then(
      (r) => j<void>(r),
    ),
  providerSettings: () =>
    fetch("/api/provider-settings").then((r) => j<ProviderSettingsPayload>(r)),
  putProviderSetting: (capability: ProviderCapability, value: ProviderSettingInput) =>
    fetch(`/api/provider-settings/${capability}`, {
      method: "PUT",
      headers: { "content-type": "application/json", ...csrfHeaders() },
      body: JSON.stringify(value),
    }).then((r) => j<ProviderSetting>(r)),
  validateProviderSetting: (capability: ProviderCapability) =>
    fetch(`/api/provider-settings/${capability}/validate`, {
      method: "POST",
      headers: csrfHeaders(),
    }).then((r) => j<ProviderValidation>(r)),
  logout: async () => {
    try {
      await fetch("/api/auth/logout", { method: "POST", headers: csrfHeaders() }).then(
        (r) => j<void>(r),
      );
    } finally {
      await purgeOfflineTenant();
    }
  },
};
