import {
  type FormEvent,
  type MutableRefObject,
  type ReactNode,
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import { api, ApiError, type AskAnswer, type AskCitation, type BookTurn, type EntityId, type ReadFrontier, type ReadSkipped } from "../api";
import { Button } from "../ui/Button";
import { companionPause, isAllowanceLine, needsPlan } from "../ui/companionPause";
import { Icon } from "../ui/Icon";
import { Moth, type MothMood } from "../ui/Moth";
import { MothliInvitation } from "../ui/MothliPlan";
import { mothliClosed, useMothliClosed, useMothliStanding } from "../ui/mothliStanding";
import { type ChapterName, PeoplePanel, StoryPanel } from "./StoryMemory";
import { activeOfflineOwner } from "../pwa/offline";
import { keepTabFocusInside } from "./focusTrap";
import type { ReaderSelection } from "./ReaderMarksPanel";
import { useCycle } from "../ui/useCycle";
import { CountAsRead } from "./CountAsRead";
import { untilNotesReady } from "./closeoutWait";
import { PASSAGE_HELP_ABSENT } from "./SelectionAssistant";
import { PanelResizeHandle } from "./PanelResize";
import "./mothli-conversation.css";
import "./companion.css";

export type SavedCompanionReply = {
  id: string;
  body: string;
  question: string;
  citations: AskCitation[];
  asOfChapter: number;
};
type AskExchange = {
  id: string;
  question: string;
  answer: AskAnswer;
  passage?: boolean;
  selectionKey?: string;
  /** Mothli spoke first (a welcome-back recap or a chapter closeout): no reader bubble. */
  fromMothli?: boolean;
  /** The server keeps this exchange. It is on screen only while the server still returns it.
   * The others (a reply about a selected passage, a recap or closeout Mothli opened with, a
   * reply from the older non-streamed path) are kept nowhere and last for this sitting. */
  stored?: boolean;
  /** For a reply that arrived here and that no load has yet found on the server: when it
   * arrived, in ms. The server stores a turn just after sending its last sentence. */
  arrived?: number;
  /** Where it sits in the conversation, in ms since the epoch. */
  at: number;
};
/** A moment where Mothli speaks first. `recap` is already in hand; `closeout` is fetched on open. */
export type CompanionSeed =
  | { id: string; kind: "recap"; label: string; text: string; asOfChapter: number }
  | { id: string; kind: "closeout"; label: string; chapter: number }
  | { id: string; kind: "ask"; prompt: string };
export type CompanionTab = "talk" | "people" | "story";
type AskSnapshot = {
  question: string;
  exchanges: AskExchange[];
  pendingQuestion: string | null;
  /** Sentences of the reply in progress that the server has already checked. */
  streaming: string;
  busy: boolean;
  error: string;
  requestToken: number;
  /** The server's copy of the conversation: not known yet, on screen, or could not be loaded.
   * Until it is known nothing the server keeps is shown. */
  history: "unknown" | "ready" | "failed";
};
export type AskSession = {
  readonly bookId: string;
  bookmark: number;
  readonly owner: string;
  readonly readingPass: string;
  scrollTop: number | null;
  /** Counts loads of the server's conversation, so a late answer to an earlier one is ignored. */
  loadToken: number;
  getSnapshot: () => AskSnapshot;
  subscribe: (listener: () => void) => () => void;
  update: (changes: Partial<AskSnapshot>) => void;
  /** Something the reader had read no longer counts as read ("I haven't read this"): take the
   * whole conversation off the screen now, and drop a reply still on its way. What may be shown
   * comes back from the server at the next load. */
  hide: () => void;
  /** Stops the request for the reply in flight, if any. Called whenever that reply is dropped
   * (a new `requestToken`), and when the session itself is let go (a new reading pass, a rewind,
   * the book closed). A streamed reply the server finishes is stored and would come back at the
   * next load; one stopped before it is done is not stored. */
  abandon: () => void;
  /** The request for the reply in flight. */
  request: AbortController | null;
  /** What the reader typed for the reply in flight, put back in the box if it never comes. */
  typed: string | null;
  /** The shortcut last sent ("Catch me up"), which Retry sends again; null when the last
   * message was typed, since its words are back in the box, edits and all. */
  lastSent: string | null;
};
const BOOK_USAGE_LIMIT_MESSAGE =
  "Mothli is paused for this book: its usage limit has been reached.";
const REQUEST_TOO_LARGE_MESSAGE = "That’s a lot of pages at once. Pick a passage and we can start there.";
const HISTORY_UNREACHABLE = "I can’t reach our earlier conversation right now, so I’m not showing it.";
const CLEAR_FAILED = "I couldn’t clear our conversation just now, so it is still here.";
/** For a stored question with no stored reply: every sentence of that reply was held back, and
 * the server's copy does not say which fixed line the reader saw then. */
const NO_REPLY_KEPT = "I didn’t have an answer for that from what you’d read.";
/** How long a reply that has just arrived is trusted before the server's copy must contain it. */
const STORE_GRACE_MS = 5000;
// The reader's unsent draft is the only thing kept in the browser, under the name that used to
// hold the whole conversation. The conversation itself is the server's: see `loadHistory`.
const STORAGE_PREFIX = "litlet.pip.conversation:";
const spokenSeeds = new Set<string>();
const KEEP_HINT_KEY = "mothli.keepHintSeen";
function storageKey(bookId: string, owner: string, pass: string) {
  return `${STORAGE_PREFIX}${encodeURIComponent(owner)}:${encodeURIComponent(bookId)}:${encodeURIComponent(pass)}`;
}
function storedDraft(key: string): string {
  try {
    const stored = JSON.parse(localStorage.getItem(key) ?? "null");
    return stored && typeof stored.question === "string" ? stored.question.slice(0, 500) : "";
  } catch {
    return "";
  }
}
function storeDraft(key: string, question: string) {
  try {
    if (question) localStorage.setItem(key, JSON.stringify({ question }));
    else localStorage.removeItem(key);
  } catch {
    /* Keep reading when storage is unavailable. */
  }
}
/** Earlier versions kept every book's conversation in the browser, where the server could not
 * hide a turn. Delete those copies, for every book and reading pass, keeping any draft. */
function dropStoredConversations() {
  try {
    const keys = Array.from({ length: localStorage.length }, (_, index) => localStorage.key(index));
    for (const key of keys) if (key?.startsWith(STORAGE_PREFIX)) storeDraft(key, storedDraft(key));
  } catch {
    /* Storage is optional. */
  }
}
/** The reply the server keeps for an exchange, as one line; empty when it kept none. */
function keptReply(answer: AskAnswer): string {
  return answer.insufficient_evidence ? "" : answer.claims.map((claim) => claim.text).join(" ");
}
const sameWords = (left: string, right: string) =>
  left.replace(/\s+/g, " ").trim() === right.replace(/\s+/g, " ").trim();
/** The server's turns as exchanges: a question with the reply that follows it. */
function storedExchanges(turns: BookTurn[], availableChapter: number | undefined): AskExchange[] {
  const pairs: { question: string | null; reply: string | null; first: BookTurn }[] = [];
  for (const turn of turns) {
    const last = pairs[pairs.length - 1];
    if (turn.role === "user") pairs.push({ question: turn.content, reply: null, first: turn });
    else if (last && last.question !== null && last.reply === null) last.reply = turn.content;
    else pairs.push({ question: null, reply: turn.content, first: turn }); // its question fell outside what was sent
  }
  return pairs.map(({ question, reply, first }) => {
    const whole = Number.isInteger(first.atom) ? first.atom : 0;
    const asOf = Math.max(0, Math.min(whole, availableChapter ?? whole));
    return {
      id: crypto.randomUUID(),
      question: question ?? "",
      stored: true,
      at: Date.parse(first.created_at ?? "") || 0,
      answer: reply === null
        ? { as_of_chapter: asOf, insufficient_evidence: true, answer_type: "conversation" as const,
            message: NO_REPLY_KEPT, claims: [], citations: [], cost: null }
        : { as_of_chapter: asOf, insufficient_evidence: false, style: "prose" as const,
            claims: [{ text: reply, citation_ids: [] }], citations: [], cost: null },
    };
  });
}
/** What the panel shows once the server has answered: the server's exchanges, and nothing else
 * that the server keeps. An exchange already on screen keeps its place and identity, so a reply
 * that streamed in here is neither drawn twice nor drawn again. */
function mergeWithServer(server: AskExchange[], shown: AskExchange[], loadStarted: number): AskExchange[] {
  const mine = shown.filter((exchange) => exchange.stored);
  const used = new Set<AskExchange>();
  const kept = server.map((incoming) => {
    const match = mine.find((exchange) =>
      !used.has(exchange) &&
      sameWords(exchange.question, incoming.question) &&
      sameWords(keptReply(exchange.answer), keptReply(incoming.answer)));
    if (!match) return incoming;
    used.add(match);
    return match.arrived === undefined ? match : { ...match, arrived: undefined };
  });
  // The one exception: a reply that arrived here moments ago, which the server may not have
  // stored when it answered this load. After that the server's word stands.
  const justArrived = mine.filter((exchange) =>
    !used.has(exchange) && exchange.arrived !== undefined && exchange.arrived >= loadStarted - STORE_GRACE_MS);
  const sitting = shown.filter((exchange) => !exchange.stored);
  return [...kept, ...justArrived, ...sitting].sort((left, right) => left.at - right.at).slice(-40);
}
/** Where the thread opens: at the latest exchange. */
function latestTop(node: HTMLElement): number {
  return node.querySelector<HTMLElement>("[data-latest-exchange]")?.offsetTop ?? 0;
}

function readableAnswer(answer: AskAnswer): string {
  return [answer.calculation, readableBookAnswer(answer)].filter(Boolean).join("\n\n");
}
function readableBookAnswer(answer: AskAnswer): string {
  if (answer.answer_type === "conversation") return answer.message ?? "Which moment do you mean?";
  if (answer.insufficient_evidence) {
    switch (answer.status) {
      case "preparing":
        return "I’m still catching up. Try again in a moment, or pick a passage.";
      case "preparation_failed":
        return "I couldn’t prepare that chapter. You can still pick a passage to discuss.";
      case "not_started":
        return "Pick a passage and let’s start there.";
      case "paratext_only":
        return "We’re still in the opening pages. Pick a passage to talk about.";
      case "ahead":
        return "We haven’t got there yet. I only know the pages you’ve read, so I can’t say what’s coming.";
      default:
        return "I can’t find that in what you’ve read so far. If it’s on these pages, point me to the bit you mean.";
    }
  }
  const body = answer.answer_type === "recap"
    ? (answer.recap ?? "")
    : answer.style === "prose"
      ? [...answer.claims.map((claim) => claim.text), answer.closing].filter(Boolean).join(" ")
      : answer.claims.map((claim) => claim.text).join("\n\n");
  return [answer.acknowledgment, body].filter(Boolean).join("\n\n");
}
function createAskSession(
  bookId: string,
  bookmark: number,
  owner: string,
  readingPass: string,
): AskSession {
  dropStoredConversations();
  const key = storageKey(bookId, owner, readingPass);
  let snapshot: AskSnapshot = {
    question: storedDraft(key),
    exchanges: [],
    pendingQuestion: null,
    streaming: "",
    busy: false,
    error: "",
    requestToken: 0,
    history: "unknown",
  };
  const listeners = new Set<() => void>();
  const session: AskSession = {
    bookId,
    bookmark,
    owner,
    readingPass,
    scrollTop: null,
    loadToken: 0,
    typed: null,
    lastSent: null,
    getSnapshot: () => snapshot,
    subscribe: (listener) => {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    request: null,
    abandon: () => {
      session.request?.abort();
      session.request = null;
    },
    update: (changes) => {
      // A new request token means the reply in flight is no longer wanted (Cancel, hide, or a
      // new question): stop its request. A reply that had already finished was stored by the
      // server and stays stored; it is the reader's, and comes back at the next load.
      if (changes.requestToken !== undefined && changes.requestToken !== snapshot.requestToken) {
        session.abandon();
        // What the reader typed for it goes back in the box, unless they have typed another.
        if (session.typed && !("question" in changes) && !snapshot.question)
          changes = { ...changes, question: session.typed };
        session.typed = null;
      }
      snapshot = { ...snapshot, ...changes };
      if ("question" in changes && owner === activeOfflineOwner()) storeDraft(key, snapshot.question);
      listeners.forEach((listener) => listener());
    },
    hide: () => {
      session.loadToken += 1;
      session.scrollTop = null;
      session.update({
        exchanges: [],
        pendingQuestion: null,
        streaming: "",
        busy: false,
        error: "",
        requestToken: snapshot.requestToken + 1,
        history: "unknown",
      });
    },
  };
  return session;
}

export function AskTheBook({
  bookId,
  bookmark,
  onNavigate,
  onClose,
  sessionRef,
  embedded = false,
  compact = false,
  readingPass = 0,
  selection,
  onClearSelection,
  onSaveReply,
  onOpenNotebook,
  chapterName,
  resize,
  initialAction,
  prepareReadContext,
  onChaptersRead,
  memoryRefresh = 0,
  finishedChapters,
  readVersion = 0,
  throughCurrentPage = false,
  seed = null,
  chapterLabel: _chapterLabel = "",
  initialTab = "talk",
  navigate = null,
  hosted = false,
}: {
  bookId: string;
  /** Hosted mode: replies come only as a stream; there is no non-streamed reply to fall back on
   * (ADR 0043), so a reply that cannot stream is a failed reply, not a second request. */
  hosted?: boolean;
  bookmark: number;
  onNavigate: (citation: AskCitation) => void;
  onClose: () => void;
  workspaceNavigation?: ReactNode;
  sessionRef?: MutableRefObject<AskSession | null>;
  embedded?: boolean;
  compact?: boolean;
  readingPass?: number | string;
  selection?: ReaderSelection | null;
  onClearSelection?: () => void;
  onSaveReply?: (reply: SavedCompanionReply) => void;
  /** Opens the Notebook on what was kept from Mothli: where a saved reply went. */
  onOpenNotebook?: () => void;
  /** The book's own chapter name for a piece ("Chapter 2"), as the footer counts them. */
  chapterName?: ChapterName;
  /** The side panel's width, when the reader may resize it (beside the book, not on a phone). */
  resize?: { width: number | null; onWidth: (width: number | null) => void };
  initialAction?: "recap" | null;
  prepareReadContext?: () => Promise<ReadFrontier | null>;
  /** The reader told Mothli, from a reply, that some chapters are read: refresh what shows it. */
  onChaptersRead?: () => void;
  /** Changes as more of the book is extracted, so the cast and story so far fetch again. */
  memoryRefresh?: number;
  /** The reader's chapter counter, for the cast and story so far to tell whether their notes are
   * still catching up with it. */
  finishedChapters?: number;
  /** Changes whenever the reader changes what counts as read other than by turning pages (a
   * chapter marked read or not read, an "I've read…" offer): the conversation is asked for again. */
  readVersion?: number;
  throughCurrentPage?: boolean;
  seed?: CompanionSeed | null;
  chapterLabel?: string;
  initialTab?: CompanionTab;
  navigate?: { tab: CompanionTab; personId: EntityId | null; nonce: number } | null;
}) {
  const localSessionRef = useRef<AskSession | null>(null);
  const retainedSession = sessionRef ?? localSessionRef;
  const owner = activeOfflineOwner();
  const pass = String(readingPass);
  // Advancing the read boundary never discards the discussion. Rewinding or resetting
  // creates a new in-memory session so late results cannot reveal a later boundary; the new
  // session shows nothing until the server's conversation has been loaded for it.
  if (
    !retainedSession.current ||
    retainedSession.current.bookId !== bookId ||
    retainedSession.current.owner !== owner ||
    retainedSession.current.readingPass !== pass ||
    retainedSession.current.bookmark > bookmark
  ) {
    // The old session's reply in flight will never be shown: stop it, so it is not stored.
    retainedSession.current?.abandon();
    retainedSession.current = createAskSession(bookId, bookmark, owner, pass);
  }
  const session = retainedSession.current;
  session.bookmark = bookmark;
  const { question, exchanges, pendingQuestion, streaming, busy, error, history } =
    useSyncExternalStore(
      session.subscribe,
      session.getSnapshot,
      session.getSnapshot,
    );
  const rootRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const conversationRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef(document.activeElement as HTMLElement | null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  const titleId = useId();
  const [confirmClear, setConfirmClear] = useState(false);
  const [liveOffer, setLiveOffer] = useState<{ id: string; offers: ReadSkipped[] } | null>(null);
  const [saveError, setSaveError] = useState("");
  const [saved, setSaved] = useState<Set<string>>(new Set());
  // A first-time reader sees one quiet line under the latest reply saying what keeping does,
  // until they keep one or say they have it. Remembered in this browser only.
  const [keepHint, setKeepHint] = useState(() => {
    try {
      return localStorage.getItem(KEEP_HINT_KEY) == null;
    } catch {
      return false;
    }
  });
  const hideKeepHint = useCallback(() => {
    setKeepHint(false);
    try {
      localStorage.setItem(KEEP_HINT_KEY, "1");
    } catch {
      /* The hint simply comes back next time. */
    }
  }, []);
  const [peopleOpen, setPeopleOpen] = useState(false);
  const [people, setPeople] = useState<{ name: string; entity_id: EntityId }[]>(
    [],
  );
  const [peopleBusy, setPeopleBusy] = useState(false);
  const [peopleError, setPeopleError] = useState("");
  const thinkingLine = useCycle(BOOK_THINKING_LINES, 2600, busy);
  const [aiDisclosure, setAiDisclosure] = useState(() => {
    try {
      return localStorage.getItem("litlet.pip.introduced") !== "yes";
    } catch {
      return true;
    }
  });
  const [tab, setTab] = useState<CompanionTab>(initialTab);
  const [focusPerson, setFocusPerson] = useState<EntityId | null>(null);
  useEffect(() => setTab(initialTab), [initialTab]);
  useEffect(() => {
    if (!navigate) return;
    setTab(navigate.tab);
    setFocusPerson(navigate.personId);
  }, [navigate?.nonce]);
  // Where the reader stands with Mothli (ADR 0045, ADR 0046). Without the plan (new, or ended):
  // one invitation in place of each tab's content, and nothing of the conversation, the cast or
  // the notes is asked for or shown.
  const standing = useMothliStanding();
  const closed = useMothliClosed();
  const wasClosed = useRef(closed);
  const modal = !embedded || compact;
  const isCurrent = () =>
    retainedSession.current === session && activeOfflineOwner() === owner;

  // The conversation shown is the server's: it hides a turn once the text it was written from
  // is no longer read, so no copy kept here may stand in for it. Returns whether it loaded.
  const loadHistory = async (): Promise<boolean> => {
    if (!isCurrent() || mothliClosed()) return false;
    const token = ++session.loadToken;
    const current = () => isCurrent() && session.loadToken === token;
    const started = Date.now();
    let answer: { read_frontier?: ReadFrontier; turns: BookTurn[] } = { turns: [] };
    try {
      if (api.bookConversation) answer = await api.bookConversation(bookId);
    } catch (reason) {
      // Mothli needs the plan: the invitation stands in the conversation's place.
      if (!current() || needsPlan(reason)) return false;
      // 404: this server keeps no conversation for a book (hosted mode), so there is none to show.
      if (!(reason instanceof ApiError && reason.status === 404)) {
        // Not reachable: show none of it, even what was on screen a moment ago.
        session.update({
          history: "failed",
          exchanges: session.getSnapshot().exchanges.filter((exchange) => !exchange.stored),
        });
        return false;
      }
    }
    if (!current()) return false;
    session.update({
      history: "ready",
      exchanges: mergeWithServer(
        storedExchanges(answer.turns ?? [], answer.read_frontier?.available_chapter),
        session.getSnapshot().exchanges,
        started,
      ),
    });
    return true;
  };
  // On open, for a new reading pass, whenever what counts as read was changed by hand, and when
  // the reader joins the plan (the conversation comes back).
  useEffect(() => {
    void loadHistory();
  }, [session, readVersion, closed]);
  // Back from the invitation: the box takes focus if the invitation had it.
  useEffect(() => {
    const active = document.activeElement;
    if (wasClosed.current && !closed && (!active || active === document.body))
      (inputRef.current ?? rootRef.current?.querySelector<HTMLElement>(".lp-companion-tabs .is-active"))?.focus({ preventScroll: true });
    wasClosed.current = closed;
  }, [closed]);

  useEffect(() => {
    inputRef.current?.focus({ preventScroll: true });
    const keydown = (event: KeyboardEvent) => {
      // Another modal sheet over this panel (the plan sheet) owns the keyboard while it is open.
      const active = document.activeElement;
      if (active && !rootRef.current?.contains(active) && active.closest("[aria-modal='true']")) return;
      if (
        event.key === "Escape" &&
        (modal || rootRef.current?.contains(document.activeElement))
      ) {
        event.preventDefault();
        event.stopPropagation();
        closeRef.current();
      } else if (modal) keepTabFocusInside(rootRef.current, event);
    };
    document.addEventListener("keydown", keydown);
    return () => {
      document.removeEventListener("keydown", keydown);
      if (triggerRef.current && document.contains(triggerRef.current))
        triggerRef.current.focus({ preventScroll: true });
    };
  }, [modal]);
  const restoreScroll = useRef(true);
  useEffect(() => {
    const node = conversationRef.current;
    if (!node) return;
    if (restoreScroll.current) {
      restoreScroll.current = false;
      if (session.scrollTop != null) {
        node.scrollTop = session.scrollTop;
        return;
      }
    }
    if (busy) node.scrollTop = node.scrollHeight;
    else node.scrollTop = latestTop(node);
  }, [exchanges.length, busy, streaming]);
  useEffect(() => {
    setPeopleOpen(false);
    setPeople([]);
    setPeopleError("");
    setPeopleBusy(false);
    setSaveError("");
    setConfirmClear(false);
  }, [bookId, owner, pass, bookmark]);

  // A message that got no reply goes back in the box, unless the reader has typed another.
  const giveBackTyped = () => {
    const typed = session.typed;
    session.typed = null;
    if (typed && !session.getSnapshot().question) session.update({ question: typed });
  };
  const send = async (
    value = session.getSnapshot().question.trim(),
    bookScope = false,
  ) => {
    if (!isCurrent() || value.length < 2 || session.getSnapshot().busy) return;
    // Mothli needs the plan: nothing is asked (a seed or a shortcut can still call this).
    if (mothliClosed()) return;
    const requestToken = session.getSnapshot().requestToken + 1;
    // Like any messaging app, what was typed leaves the box the moment it's sent; a shortcut
    // ("Catch me up") leaves whatever the reader had typed alone.
    const fromBox = value === session.getSnapshot().question.trim();
    session.update({
      ...(fromBox ? { question: "" } : {}),
      busy: true,
      error: "",
      pendingQuestion: value,
      streaming: "",
      requestToken,
    });
    session.typed = fromBox ? value : null;
    session.lastSent = fromBox ? null : value;
    const request = new AbortController();
    session.request = request;
    const isCurrentRequest = () =>
      isCurrent() && session.getSnapshot().requestToken === requestToken;
    const scopeSelection = bookScope ? null : selection;
    const selectionKey = scopeSelection
      ? `${scopeSelection.anchor.cfi}:${scopeSelection.text}`
      : undefined;
    try {
      let result: AskAnswer;
      let kept = false; // whether the server stores this exchange: only a streamed reply is
      if (scopeSelection) {
        const selected = await api.selectionAction(bookId, {
          action: "explain",
          text: scopeSelection.text,
          atom: scopeSelection.anchor.atom,
          cfi: scopeSelection.anchor.cfi,
          question: value,
          history: session
            .getSnapshot()
            .exchanges.filter(
              (exchange) =>
                exchange.selectionKey === selectionKey &&
                !exchange.answer.insufficient_evidence,
            )
            .flatMap((exchange) => [
              {
                role: "user" as const,
                content: exchange.question.slice(0, 2000),
                as_of_chapter: exchange.answer.as_of_chapter,
              },
              {
                role: "assistant" as const,
                content: readableAnswer(exchange.answer).slice(0, 2000),
                as_of_chapter: exchange.answer.as_of_chapter,
              },
            ])
            .slice(-8),
        });
        result = {
          as_of_chapter: selected.as_of_chapter,
          acknowledgment: selected.acknowledgment,
          insufficient_evidence: selected.insufficient_evidence,
          ...(selected.message ? {answer_type: "conversation" as const, message: selected.message} : {}),
          claims: selected.text
            ? [{ text: selected.text, citation_ids: [1] }]
            : [],
          citations: selected.citation ? [selected.citation] : [],
          cost: selected.cost,
        };
        if (
          selected.citation &&
          (selected.citation.ordinal !== scopeSelection.anchor.atom ||
            selected.citation.cfi !== scopeSelection.anchor.cfi)
        )
          throw new Error(
            "I couldn’t check that passage. Please select it again.",
          );
      } else {
        const readPoint = prepareReadContext ? await prepareReadContext() : null;
        if (!isCurrentRequest()) return;
        const requestBookmark = Math.max(bookmark, readPoint?.available_chapter ?? 0);
        // Sentence by sentence when the server offers it; each one arrives already checked.
        // The server writes from its own record of the conversation, so none is sent.
        const streamed = readPoint && api.askBookStream
          ? await api.askBookStream(bookId, value, requestBookmark, readPoint, (sentence) => {
              if (isCurrentRequest())
                session.update({ streaming: [session.getSnapshot().streaming, sentence].filter(Boolean).join(" ") });
            }, request.signal)
          : null;
        if (!isCurrentRequest()) return;
        // Hosted mode has nothing else to ask: shown as any reply that could not be had.
        if (hosted && streamed === null) throw new Error("no streamed reply");
        kept = streamed !== null;
        // The older, non-streamed path keeps nothing on the server and still reads the earlier
        // conversation from the request: it is sent what is on this panel now.
        const earlier = () => session
          .getSnapshot()
          .exchanges.filter(
            (exchange) =>
              !exchange.passage &&
              !!exchange.question &&
              exchange.answer.as_of_chapter <= requestBookmark &&
              !exchange.answer.insufficient_evidence,
          )
          .flatMap((exchange) => [
            {
              role: "user" as const,
              content: exchange.question.slice(0, 2000),
              as_of_chapter: exchange.answer.as_of_chapter,
            },
            {
              role: "assistant" as const,
              content: readableAnswer(exchange.answer).slice(0, 2000),
              as_of_chapter: exchange.answer.as_of_chapter,
            },
          ])
          .slice(-8);
        result = streamed ?? (readPoint
          ? await api.askBook(bookId, value, requestBookmark, earlier(), readPoint, request.signal)
          : await api.askBook(bookId, value, requestBookmark, earlier(), undefined, request.signal));
        if (
          result.as_of_chapter > requestBookmark ||
          result.citations.some((citation) => citation.ordinal > requestBookmark) ||
          (readPoint && result.read_frontier?.id !== readPoint.id)
        )
          throw new Error(
            "I couldn’t check that reply against your read pages. Try again?",
          );
      }
      if (!isCurrentRequest()) return;
      // An offer to count skipped chapters belongs to this sitting, not to the saved conversation.
      const { offers, ...shown } = result;
      const exchangeId = crypto.randomUUID();
      const now = Date.now();
      setLiveOffer(offers?.length ? { id: exchangeId, offers } : null);
      session.update({
        exchanges: [
          ...session.getSnapshot().exchanges,
          {
            id: exchangeId,
            question: value,
            answer: shown,
            passage: !!scopeSelection,
            selectionKey,
            at: now,
            ...(kept ? { stored: true, arrived: now } : {}),
          },
        ].slice(-40),
        pendingQuestion: null,
      });
      session.typed = null;
      setPeopleOpen(false);
    } catch (reason) {
      // Mothli needs the plan: the invitation stands where the reply would be, never an error
      // line. The question goes back in the box for when the reader is back.
      if (isCurrentRequest()) giveBackTyped();
      if (isCurrentRequest() && !needsPlan(reason)) {
        const message =
          companionPause(reason)?.line ??
          (reason instanceof ApiError && reason.status === 413
            ? REQUEST_TOO_LARGE_MESSAGE
            : reason instanceof ApiError && reason.status === 429
            ? /cost ceiling/i.test(reason.message)
              ? BOOK_USAGE_LIMIT_MESSAGE
              : "Mothli needs a short break. Try again shortly."
            : reason instanceof ApiError && reason.status === 502 && /cleared against (your read pages|the selected passage)/i.test(reason.message)
            ? scopeSelection
              ? "I couldn’t verify that from the selected passage. Try selecting a little more context."
              : "I couldn’t back that up from your pages, so I’d rather not guess. Try naming the person or moment you mean."
            : reason instanceof ApiError && reason.status === 409
            ? "Your reading position changed. Please try again from this page."
            : scopeSelection && reason instanceof ApiError && reason.status === 404
            ? PASSAGE_HELP_ABSENT
            : reason instanceof ApiError && reason.code === "ask_timeout"
            ? "That took too long, so I stopped. Try again?"
            : "I couldn’t get a reply. Try again?");
        session.update({ error: message });
      }
    } finally {
      if (session.request === request) session.request = null;
      if (isCurrentRequest())
        session.update({ busy: false, pendingQuestion: null, streaming: "" });
    }
  };
  const openPeople = async () => {
    if (peopleBusy) return;
    let expectedBookmark=bookmark;
    const currentPeople = () => isCurrent() && session.bookmark === expectedBookmark;
    setPeopleOpen(true);
    setPeopleBusy(true);
    setPeopleError("");
    try {
      const graph = await api.graph(bookId, bookmark);
      if (!currentPeople() || graph.as_of_chapter>bookmark) return;
      if (graph.characters.length) {
        setPeople(graph.characters.map((person) => ({name:person.canonical_name, entity_id:person.entity_id})));
      } else if (prepareReadContext) {
        const point=await prepareReadContext();
        if (!isCurrent() || !point) return;
        expectedBookmark=Math.max(bookmark,point.available_chapter);
        if (!currentPeople()) return;
        const result=await api.visiblePeople(bookId,point);
        if(currentPeople() && result.read_frontier.id===point.id)
          setPeople(result.names.map((name,index)=>({name,entity_id:-(index+1)})));
      } else setPeople([]);
    } catch (reason) {
      // A 404: this server keeps no names for the book (hosted mode, for now). Mothli needing
      // the plan: the invitation, no line.
      if (currentPeople() && !needsPlan(reason))
        setPeopleError(reason instanceof ApiError && reason.status === 404
          ? "I don’t keep a list of names for this book yet. Ask me about anyone in your own words."
          : "Couldn’t find the names. Try again?");
    } finally {
      if (currentPeople()) setPeopleBusy(false);
    }
  };
  const submit = (event: FormEvent) => {
    event.preventDefault();
    void send();
  };
  const initialActionRef = useRef(false);
  useEffect(() => {
    if (initialAction === "recap" && !initialActionRef.current) {
      initialActionRef.current = true;
      if (!session.getSnapshot().busy) void send("Catch me up");
    }
  }, [initialAction, session]);
  // On a phone Mothli is a sheet over the page, and it should feel like an app's own screen:
  // - the page behind can't move: the body is pinned, and a swipe anywhere that isn't the
  //   conversation (or another list that can still scroll that way) is held, so iOS never pans the
  //   page under the keyboard and the sheet never has to chase it;
  // - the sheet fills exactly what's visible above the keyboard, following it frame by frame while
  //   it slides, moved with a transform (no relayout) rather than a new top;
  // - the canvas behind Safari's floating bars takes the sheet's colour, so no strip of page shows.
  // The latest words stay in view as the sheet gets shorter.
  useEffect(() => {
    if (!compact) return;
    const root = rootRef.current;
    const html = document.documentElement;
    const body = document.body;
    const pageY = window.scrollY;
    const held = {
      htmlOverflow: html.style.overflow, htmlBackground: html.style.backgroundColor,
      bodyOverflow: body.style.overflow, bodyBackground: body.style.backgroundColor,
      bodyPosition: body.style.position, bodyTop: body.style.top, bodyWidth: body.style.width,
      overscroll: body.style.overscrollBehavior,
    };
    const surface = root ? getComputedStyle(root).backgroundColor : "";
    html.style.overflow = "hidden";
    body.style.overflow = "hidden";
    body.style.overscrollBehavior = "none";
    body.style.position = "fixed";
    body.style.top = `-${pageY}px`;
    body.style.width = "100%";
    if (surface) {
      html.style.backgroundColor = surface;
      body.style.backgroundColor = surface;
    }
    const themeColor = document.querySelector<HTMLMetaElement>('meta[name="theme-color"]');
    const heldTheme = themeColor?.content;
    if (themeColor && surface) themeColor.content = surface;

    const viewport = window.visualViewport;
    let lastHeight = -1;
    let lastTop = -1;
    const fit = () => {
      if (!root || !viewport) return;
      const height = Math.round(viewport.height);
      const top = Math.round(Math.max(0, viewport.offsetTop));
      if (height === lastHeight && top === lastTop) return;
      const thread = conversationRef.current;
      const atEnd = thread ? thread.scrollHeight - thread.scrollTop - thread.clientHeight < 24 : false;
      root.style.setProperty("--mothli-viewport-height", `${height}px`);
      root.style.setProperty("--mothli-viewport-top", "0px");
      root.style.transform = top ? `translate3d(0, ${top}px, 0)` : "";
      if (thread && atEnd && height < lastHeight) thread.scrollTop = thread.scrollHeight;
      lastHeight = height;
      lastTop = top;
    };
    let frame = 0;
    let until = 0;
    const follow = () => {
      fit();
      frame = performance.now() < until ? requestAnimationFrame(follow) : 0;
    };
    const followFor = (ms: number) => {
      until = Math.max(until, performance.now() + ms);
      if (!frame) frame = requestAnimationFrame(follow);
    };
    const followKeyboard = () => followFor(900);
    const nextFrame = () => followFor(0);

    // A swipe is held unless something under the finger can still scroll that way.
    let lastX = 0;
    let lastY = 0;
    const canScroll = (from: EventTarget | null, dx: number, dy: number) => {
      for (let node = from instanceof Element ? from : null; node && node !== root?.parentElement; node = node.parentElement) {
        const style = getComputedStyle(node);
        if (Math.abs(dy) >= Math.abs(dx) && /(auto|scroll)/.test(style.overflowY) && node.scrollHeight > node.clientHeight + 1) {
          if (dy > 0 && node.scrollTop > 0) return true;
          if (dy < 0 && node.scrollTop + node.clientHeight < node.scrollHeight - 1) return true;
        }
        if (Math.abs(dx) > Math.abs(dy) && /(auto|scroll)/.test(style.overflowX) && node.scrollWidth > node.clientWidth + 1) {
          if (dx > 0 && node.scrollLeft > 0) return true;
          if (dx < 0 && node.scrollLeft + node.clientWidth < node.scrollWidth - 1) return true;
        }
      }
      return false;
    };
    const touchStart = (event: TouchEvent) => {
      lastX = event.touches[0]?.clientX ?? 0;
      lastY = event.touches[0]?.clientY ?? 0;
    };
    const touchMove = (event: TouchEvent) => {
      if (event.touches.length !== 1) return; // pinch-to-zoom stays the reader's
      const x = event.touches[0].clientX;
      const y = event.touches[0].clientY;
      const dx = x - lastX;
      const dy = y - lastY;
      lastX = x;
      lastY = y;
      if (!dx && !dy) return;
      if (!canScroll(event.target, dx, dy) && event.cancelable) event.preventDefault();
    };

    fit();
    viewport?.addEventListener("resize", followKeyboard);
    viewport?.addEventListener("scroll", nextFrame);
    document.addEventListener("focusin", followKeyboard);
    document.addEventListener("focusout", followKeyboard);
    document.addEventListener("touchstart", touchStart, { passive: true });
    document.addEventListener("touchmove", touchMove, { passive: false });
    return () => {
      cancelAnimationFrame(frame);
      viewport?.removeEventListener("resize", followKeyboard);
      viewport?.removeEventListener("scroll", nextFrame);
      document.removeEventListener("focusin", followKeyboard);
      document.removeEventListener("focusout", followKeyboard);
      document.removeEventListener("touchstart", touchStart);
      document.removeEventListener("touchmove", touchMove);
      if (root) root.style.transform = "";
      if (themeColor && heldTheme != null) themeColor.content = heldTheme;
      html.style.overflow = held.htmlOverflow;
      html.style.backgroundColor = held.htmlBackground;
      body.style.overflow = held.bodyOverflow;
      body.style.backgroundColor = held.bodyBackground;
      body.style.position = held.bodyPosition;
      body.style.top = held.bodyTop;
      body.style.width = held.bodyWidth;
      body.style.overscrollBehavior = held.overscroll;
      if (pageY) window.scrollTo(0, pageY);
    };
  }, [compact]);
  // Mothli speaks first: a recap already in hand, or a chapter closeout fetched once on open.
  useEffect(() => {
    // The panel unmounts when closed, so a ref would forget: reopening must not replay the
    // last seed (a second paid closeout, a repeated question, or a duplicated recap).
    if (!seed || spokenSeeds.has(seed.id)) return;
    spokenSeeds.add(seed.id);
    setTab("talk");
    if (seed.kind === "ask") {
      void send(seed.prompt, true);
      return;
    }
    if (seed.kind === "recap") {
      session.update({
        exchanges: [
          ...session.getSnapshot().exchanges,
          {
            id: seed.id,
            question: seed.label,
            fromMothli: true,
            at: Date.now(),
            answer: {
              as_of_chapter: Math.min(seed.asOfChapter, bookmark),
              insufficient_evidence: false,
              claims: [],
              citations: [],
              cost: null,
              answer_type: "recap" as const,
              recap: seed.text,
            },
          },
        ].slice(-40),
      });
      return;
    }
    if (session.getSnapshot().busy) return;
    const requestToken = session.getSnapshot().requestToken + 1;
    session.update({ busy: true, error: "", pendingQuestion: null, requestToken });
    session.lastSent = null; // Retry after this sends what is in the box, never an older shortcut
    const isCurrentRequest = () => isCurrent() && session.getSnapshot().requestToken === requestToken;
    // A server with no chapter closeout (hosted mode, where Mothli's notes go into the chat
    // instead): ask the chat about the chapter, as the reader would have.
    let askInstead = false;
    // Notes on a just-finished chapter are still being taken: wait for them quietly (closeoutWait).
    void untilNotesReady(() => api.chapterCloseout(bookId, seed.chapter), isCurrentRequest)
      .then((result) => {
        if (!isCurrentRequest()) return;
        if (result.as_of_chapter > bookmark || result.citations.some((citation) => citation.ordinal > bookmark))
          throw new Error("unverified");
        session.update({
          exchanges: [
            ...session.getSnapshot().exchanges,
            { id: crypto.randomUUID(), question: seed.label, answer: result, fromMothli: true, at: Date.now() },
          ].slice(-40),
        });
      })
      .catch((reason: unknown) => {
        if (!isCurrentRequest()) return;
        if (reason instanceof ApiError && reason.status === 404) {
          askInstead = true;
          return;
        }
        session.update({
          error:
            companionPause(reason)?.line ??
            (reason instanceof ApiError && reason.status === 409
              ? "I’m still taking notes on that chapter. Ask me again in a minute."
              : reason instanceof ApiError && reason.status === 429
                ? BOOK_USAGE_LIMIT_MESSAGE
                : "I couldn’t gather my thoughts on that chapter. Try again?"),
        });
      })
      .finally(() => {
        if (!isCurrentRequest()) return;
        session.update({ busy: false });
        if (askInstead) void send(`What did you notice in ${seed.label.replace(/^After /, "")}?`, true);
      });
  }, [seed?.id]);

  const askAbout = (prompt: string) => {
    setTab("talk");
    onClearSelection?.();
    void send(prompt, true);
    inputRef.current?.focus({ preventScroll: true });
  };

  // Which stored turns may be shown is the server's decision, made from what is read now. The
  // replies kept only here are still held to the chapter the reader has reached.
  const visibleExchanges = exchanges.filter(
    (exchange) => exchange.stored || exchange.passage || exchange.answer.as_of_chapter <= bookmark,
  );
  const mood: MothMood = busy ? "thinking" : error ? "sleepy" : !visibleExchanges.length ? "curious" : "idle";
  const starters = selection
    ? ["What’s going on here?", "Why does this matter?"]
    : bookmark > 0
      ? ["What just happened?", "What have I missed?"]
      : [];
  const chips = (visibleExchanges.length ? [] : starters).slice(0, 3);
  const keepable = (exchange: AskExchange) =>
    !exchange.answer.insufficient_evidence && exchange.answer.answer_type !== "conversation";
  const lastKeepable = visibleExchanges.reduce((last, exchange, index) => (keepable(exchange) ? index : last), -1);

  return (
    <div
      className={`mothli-panel mothli-conversation lp-companion${embedded ? " mothli-conversation--embedded" : " mothli-conversation--standalone"}${compact ? " mothli-conversation--compact" : ""}`}
      role={modal ? "dialog" : "complementary"}
      aria-modal={modal || undefined}
      aria-labelledby={titleId}
      ref={rootRef}
      data-tab={tab}
    >
      {resize && embedded && !compact && <PanelResizeHandle width={resize.width} onWidth={resize.onWidth} />}
      <header className="lp-companion-head">
        <div className="lp-companion-id">
          <span className="lp-moth-ring"><Moth mood={mood} size={42} glow={false} /></span>
          <div>
            <h2 id={titleId} className="lp-voice">Mothli</h2>
            <span key={busy ? thinkingLine : "rest"} className="lp-companion-status">{busy ? thinkingLine : selection ? "Talking about the passage you picked" : "Knows the story up to here"}</span>
          </div>
        </div>
        <div className="lp-companion-head-actions">
          {!closed && (
            <Button
              variant="ghost"
              className="mothli-menu"
              aria-label="Conversation options"
              title="Conversation options"
              onClick={() => setConfirmClear(!confirmClear)}
            >
              <Icon name="more" size={17} />
            </Button>
          )}
          <Button
            variant="ghost"
            className="mothli-close"
            aria-label="Back to reading"
            title="Back to reading"
            onClick={onClose}
          >
            <Icon name={compact ? "arrowLeft" : "close"} size={18} />
            {compact && <span>Read</span>}
          </Button>
        </div>
        <nav className="lp-companion-tabs" aria-label="Mothli views">
          {(
            [
              ["talk", "Chat"],
              ["people", "Cast"],
              ["story", "Story so far"],
            ] as const
          ).map(([key, label]) => (
            <button
              key={key}
              type="button"
              aria-pressed={tab === key}
              className={tab === key ? "is-active" : undefined}
              onClick={() => {
                setTab(key);
                if (key !== "people") setFocusPerson(null);
              }}
            >
              {label}
            </button>
          ))}
        </nav>
      </header>
      {closed ? (
        <div className="lp-companion-body">
          <MothliInvitation key={tab} why={standing?.why ?? null} scope={rootRef} />
        </div>
      ) : tab === "people" ? (
        <div className="lp-companion-body">
          <PeoplePanel
            bookId={bookId}
            bookmark={bookmark}
            place={finishedChapters}
            refresh={memoryRefresh}
            focusId={focusPerson}
            chapterName={chapterName}
            onAsk={(name) => askAbout(`Remind me who ${name} is.`)}
          />
        </div>
      ) : tab === "story" ? (
        <div className="lp-companion-body">
          <StoryPanel
            bookId={bookId}
            bookmark={bookmark}
            place={finishedChapters}
            refresh={memoryRefresh}
            chapterName={chapterName}
            onOpenPerson={(id) => {
              setFocusPerson(id);
              setTab("people");
            }}
            onAskChapter={(title) => askAbout(`What stood out in ${title}?`)}
          />
        </div>
      ) : (
        <>
          <div
            className="mothli-thread lp-thread"
            ref={conversationRef}
            onScroll={(event) => {
              session.scrollTop = event.currentTarget.scrollTop;
            }}
            role="region"
            aria-label="Book conversation"
          >
            {confirmClear && (
              <div className="mothli-clear lp-clear">
                <span>Start a fresh chat? Replies you kept stay in your Notebook.</span>
                <Button
                  variant="ghost"
                  onClick={() => {
                    session.loadToken += 1; // a load already on its way must not bring it back
                    session.update({
                      question: "",
                      exchanges: [],
                      pendingQuestion: null,
                      streaming: "",
                      busy: false,
                      error: "",
                      requestToken: session.getSnapshot().requestToken + 1,
                      history: "ready",
                    });
                    session.scrollTop = null;
                    // The conversation is the server's record: clear it there, then show what
                    // the server has. If the clear did not get through, that is the turns again.
                    void (async () => {
                      let cleared = true;
                      try {
                        await api.clearConversation?.(bookId);
                      } catch {
                        cleared = false;
                      }
                      if (!isCurrent()) return;
                      const loaded = await loadHistory();
                      if (!cleared && loaded && isCurrent()) session.update({ error: CLEAR_FAILED });
                    })();
                    setConfirmClear(false);
                    setPeopleOpen(false);
                    inputRef.current?.focus();
                  }}
                >
                  Clear
                </Button>
                <Button variant="ghost" onClick={() => setConfirmClear(false)}>
                  Keep
                </Button>
              </div>
            )}
            {history === "failed" && (
              <div className="mothli-clear lp-clear" role="group" aria-label="Earlier conversation">
                <span>{HISTORY_UNREACHABLE}</span>
                <Button variant="ghost" onClick={() => void loadHistory()}>
                  Try again
                </Button>
              </div>
            )}
            {!visibleExchanges.length && !busy && history === "ready" && (
              <div className="lp-hello">
                <p className="lp-voice lp-hello-line">
                  {selection
                    ? "Ooh, good pick. What do you want to know about it?"
                    : bookmark > 0
                      ? "Hi! I’ve been reading along with you. Forgot someone, lost the thread, or spotted something odd? Ask me."
                      : "Hi! I’m Mothli. I’ll read along with you and remember the story, so you don’t have to."}
                </p>
                {aiDisclosure && (
                  <p className="lp-hello-fine">
                    I only know the pages you’ve already read, so I can’t spoil what’s ahead.{" "}
                    <button
                      type="button"
                      aria-label="Dismiss introduction"
                      onClick={() => {
                        setAiDisclosure(false);
                        try {
                          localStorage.setItem("litlet.pip.introduced", "yes");
                        } catch {
                          /* Optional preference. */
                        }
                      }}
                    >
                      Got it
                    </button>
                  </p>
                )}
              </div>
            )}
            {visibleExchanges.map((exchange, index) => (
              <section
                className={`mothli-exchange lp-exchange${exchange.fromMothli ? " lp-exchange--mothli-first" : ""}`}
                aria-label={exchange.fromMothli ? exchange.question : exchange.question ? `Question ${index + 1}` : "Earlier reply"}
                data-latest-exchange={index === visibleExchanges.length - 1 || undefined}
                key={exchange.id}
              >
                {exchange.fromMothli ? (
                  <p className="lp-moment-label">{exchange.question}</p>
                ) : (
                  exchange.question && <p className="mothli-question lp-you">{exchange.question}</p>
                )}
                <div className="mothli-reply lp-reply">
                  <div className="lp-reply-body">
                    <p>{readableAnswer(exchange.answer)}</p>
                    {liveOffer?.id === exchange.id && (
                      <CountAsRead offers={liveOffer.offers} onCounted={onChaptersRead} />
                    )}
                    {keepable(exchange) && (
                      <div className="mothli-reply-actions lp-reply-actions">
                        {exchange.answer.citations.length > 0 && (
                          <details className="mothli-sources lp-sources">
                            <summary>
                              <Icon name="book" size={14} />
                              Pages
                            </summary>
                            <div>
                              {exchange.answer.citations.map((citation) => (
                                <button
                                  key={citation.id}
                                  type="button"
                                  onClick={() => {
                                    if (isCurrent()) onNavigate(citation);
                                  }}
                                >
                                  <span>
                                    {citation.title || "Passage"}
                                    <Icon name="arrowRight" size={14} />
                                  </span>
                                  <q>{citation.excerpt}</q>
                                </button>
                              ))}
                            </div>
                          </details>
                        )}
                        {onSaveReply && (saved.has(exchange.id) ? (
                          <span className="lp-kept" role="status">
                            <Icon name="check" size={14} />
                            <span>Kept in your Notebook</span>
                            {onOpenNotebook && (
                              <button type="button" className="lp-kept-open" onClick={() => isCurrent() && onOpenNotebook()}>
                                Open Notebook
                              </button>
                            )}
                          </span>
                        ) : (
                          <Button
                            variant="ghost"
                            className="mothli-save lp-save"
                            icon="bookmark"
                            title="Keep this reply in your Notebook to read again later"
                            onClick={() => {
                              if (!isCurrent()) return;
                              try {
                                onSaveReply({
                                  id: exchange.id,
                                  body: readableAnswer(exchange.answer),
                                  question: exchange.question,
                                  citations: exchange.answer.citations,
                                  asOfChapter: exchange.answer.as_of_chapter,
                                });
                                setSaved(new Set([...saved, exchange.id]));
                                setSaveError("");
                                hideKeepHint();
                              } catch {
                                setSaveError("Couldn’t keep that reply. Try Keep in Notebook again.");
                              }
                            }}
                          >
                            Keep in Notebook
                          </Button>
                        ))}
                      </div>
                    )}
                    {onSaveReply && keepHint && !saved.has(exchange.id) && index === lastKeepable && (
                      <div className="lp-keep-hint">
                        <span>Like a reply? Keep it in your Notebook and find it there any time.</span>
                        <button type="button" onClick={hideKeepHint}>Got it</button>
                      </div>
                    )}
                  </div>
                </div>
              </section>
            ))}
            {busy && (
              <section className="mothli-exchange lp-exchange" aria-label="Sending message">
                {pendingQuestion && <p className="mothli-question lp-you">{pendingQuestion}</p>}
                {streaming && (
                  <div className="mothli-reply lp-reply" aria-live="polite">
                    <div className="lp-reply-body">
                      <p>{streaming}</p>
                    </div>
                  </div>
                )}
                <div className="lp-thinking">
                  <span className="lp-thinking-dots" aria-hidden="true">
                    <i />
                    <i />
                    <i />
                  </span>
                  <button
                    className="mothli-cancel"
                    type="button"
                    onClick={() => {
                      session.update({
                        busy: false,
                        pendingQuestion: null,
                        streaming: "",
                        requestToken: session.getSnapshot().requestToken + 1,
                      });
                    }}
                  >
                    Cancel
                  </button>
                </div>
              </section>
            )}
            {peopleOpen && (
              <div className="mothli-people lp-people-pick">
                <div>
                  <span className="lp-voice">Who was it?</span>
                  <button type="button" aria-label="Close people" onClick={() => setPeopleOpen(false)}>
                    <Icon name="close" size={15} />
                  </button>
                </div>
                {peopleBusy ? (
                  <span>Finding names…</span>
                ) : peopleError ? (
                  <button type="button" onClick={() => void openPeople()}>
                    {peopleError}
                  </button>
                ) : people.length ? (
                  <div className="lp-people-grid">
                    {people.slice(0, 24).map((person) => (
                      <Button
                        key={person.entity_id}
                        variant="ghost"
                        disabled={busy}
                        onClick={() => {
                          onClearSelection?.();
                          void send(`Remind me who ${person.name} is.`, true);
                          setPeopleOpen(false);
                          inputRef.current?.focus();
                        }}
                      >
                        <span className="mothli-person-monogram" aria-hidden="true">
                          {person.name.trim().charAt(0)}
                        </span>
                        {person.name}
                      </Button>
                    ))}
                    {people.length > 24 && (
                      <button
                        type="button"
                        className="lp-see-all"
                        onClick={() => {
                          setPeopleOpen(false);
                          setTab("people");
                        }}
                      >
                        See all {people.length} in People
                      </button>
                    )}
                  </div>
                ) : (
                  <p>No names yet. Select a name in your book.</p>
                )}
              </div>
            )}
          </div>
          <div className="mothli-bottom lp-bottom">
            <div className="mothli-shortcuts lp-starters" aria-label="Conversation shortcuts">
              <Button variant="ghost" icon="book" className="lp-chip lp-chip--lamp" disabled={busy || !!selection} onClick={() => void send("Catch me up")}>
                Catch me up
              </Button>
              <Button variant="ghost" icon="search" className="lp-chip" disabled={busy} onClick={() => void openPeople()}>
                Who was that?
              </Button>
              {chips.map((prompt) => (
                <button key={prompt} type="button" className="lp-chip" disabled={busy} onClick={() => void send(prompt)}>
                  {prompt}
                </button>
              ))}
            </div>
            {selection && (
              <div className="mothli-selection lp-selection">
                <q>{selection.text}</q>
                {onClearSelection && (
                  <button type="button" aria-label="Remove passage context" disabled={busy} onClick={onClearSelection}>
                    <Icon name="close" size={14} />
                  </button>
                )}
              </div>
            )}
            <div className="mothli-status lp-status" role="status" aria-live="polite">
              {saveError ||
                (busy ? (
                  <span className="sr-only">Mothli is checking your pages.</span>
                ) : (
                  error && (
                    <>
                      <span>{error}</span>
                      {error !== BOOK_USAGE_LIMIT_MESSAGE && error !== REQUEST_TOO_LARGE_MESSAGE && error !== CLEAR_FAILED && !isAllowanceLine(error) && (
                        <button type="button" onClick={() => void send(session.lastSent ?? undefined)}>
                          Retry
                        </button>
                      )}
                    </>
                  )
                ))}
            </div>
            <form className="mothli-composer lp-composer" onSubmit={submit}>
              <label htmlFor={`${titleId}-message`} className="sr-only">
                Message Mothli
              </label>
              <textarea
                id={`${titleId}-message`}
                ref={inputRef}
                rows={1}
                maxLength={500}
                placeholder={selection ? "Ask about this passage…" : "Ask Mothli anything…"}
                value={question}
                // read-only, not disabled, while Mothli answers: a disabled box drops focus, and on
                // a phone that closes the keyboard after every message and jolts the whole sheet
                readOnly={busy}
                aria-busy={busy || undefined}
                onChange={(event) => session.update({ question: event.currentTarget.value })}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
                    event.preventDefault();
                    void send();
                  }
                }}
              />
              <Button
                type="submit"
                variant="primary"
                aria-label="Send message"
                className="mothli-send lp-send"
                disabled={busy || question.trim().length < 2}
              >
                <Icon name="arrowRight" size={18} />
              </Button>
            </form>
            <p className="lp-spoiler-line" title="Mothli only uses the pages you’ve read">
              <Icon name="shield" size={12} />
              {selection ? "Only this passage" : throughCurrentPage ? "Only up to this page. No spoilers." : "Only what you’ve read. No spoilers."}
            </p>
          </div>
        </>
      )}
    </div>
  );
}

/** What Mothli is up to while a reply in the book is on its way. */
const BOOK_THINKING_LINES = [
  "Thinking it over…",
  "Flipping back a few pages…",
  "Checking what we’ve read so far…",
  "Got it. Writing it down…",
];
