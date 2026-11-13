import {
  type FormEvent,
  type MutableRefObject,
  type ReactNode,
  useEffect,
  useId,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import { api, ApiError, type AskAnswer, type AskCitation, type ReadFrontier } from "../api";
import { Button } from "../ui/Button";
import { Icon } from "../ui/Icon";
import { Moth, type MothMood } from "../ui/Moth";
import { PeoplePanel, StoryPanel } from "./StoryMemory";
import { activeOfflineOwner } from "../pwa/offline";
import { keepTabFocusInside } from "./focusTrap";
import type { ReaderSelection } from "./ReaderMarksPanel";
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
  busy: boolean;
  error: string;
  requestToken: number;
};
export type AskSession = {
  readonly bookId: string;
  bookmark: number;
  readonly owner: string;
  readonly readingPass: string;
  scrollTop: number | null;
  getSnapshot: () => AskSnapshot;
  subscribe: (listener: () => void) => () => void;
  update: (changes: Partial<AskSnapshot>) => void;
};
const BOOK_USAGE_LIMIT_MESSAGE =
  "Mothli is paused for this book: its usage limit has been reached.";
const REQUEST_TOO_LARGE_MESSAGE = "That’s a lot of pages at once. Pick a passage and we can start there.";
// Legacy storage namespace preserves existing conversations after the Mothli rename.
const STORAGE_PREFIX = "litlet.pip.conversation:";
function storageKey(bookId: string, owner: string, pass: string) {
  return `${STORAGE_PREFIX}${encodeURIComponent(owner)}:${encodeURIComponent(bookId)}:${encodeURIComponent(pass)}`;
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
      default:
        return "I couldn’t find enough in your read pages to answer that. Which passage did you mean?";
    }
  }
  const body = answer.answer_type === "recap"
    ? (answer.recap ?? "")
    : answer.claims.map((claim) => claim.text).join("\n\n");
  return [answer.acknowledgment, body].filter(Boolean).join("\n\n");
}
function isStoredExchange(value: unknown): value is AskExchange {
  if (!value || typeof value !== "object") return false;
  const item = value as AskExchange;
  return (
    typeof item.id === "string" &&
    typeof item.question === "string" &&
    !!item.answer &&
    Number.isInteger(item.answer.as_of_chapter) &&
    typeof item.answer.insufficient_evidence === "boolean" &&
    Array.isArray(item.answer.claims) &&
    item.answer.claims.every(
      (claim) =>
        typeof claim.text === "string" && Array.isArray(claim.citation_ids),
    ) &&
    Array.isArray(item.answer.citations) &&
    item.answer.citations.every(
      (citation) =>
        typeof citation.id === "number" &&
        typeof citation.ordinal === "number" &&
        typeof citation.href === "string" &&
        typeof citation.excerpt === "string",
    )
  );
}
function createAskSession(
  bookId: string,
  bookmark: number,
  owner: string,
  readingPass: string,
): AskSession {
  let snapshot: AskSnapshot = {
    question: "",
    exchanges: [],
    pendingQuestion: null,
    busy: false,
    error: "",
    requestToken: 0,
  };
  const key = storageKey(bookId, owner, readingPass);
  try {
    const stored = JSON.parse(localStorage.getItem(key) ?? "null");
    if (
      stored &&
      typeof stored.question === "string" &&
      Array.isArray(stored.exchanges)
    )
      snapshot = {
        ...snapshot,
        question: stored.question.slice(0, 500),
        exchanges: stored.exchanges
          .filter(isStoredExchange)
          .filter(
            (exchange: AskExchange) =>
              exchange.answer.as_of_chapter <= bookmark || exchange.passage,
          )
          .slice(-40),
      };
  } catch {
    /* Storage is optional; the mounted session remains usable. */
  }
  const listeners = new Set<() => void>();
  return {
    bookId,
    bookmark,
    owner,
    readingPass,
    scrollTop: null,
    getSnapshot: () => snapshot,
    subscribe: (listener) => {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    update: (changes) => {
      snapshot = { ...snapshot, ...changes };
      if (owner === activeOfflineOwner()) {
        try {
          localStorage.setItem(
            key,
            JSON.stringify({
              question: snapshot.question,
              exchanges: snapshot.exchanges.slice(-40),
            }),
          );
        } catch {
          /* Keep reading when storage is unavailable. */
        }
      }
      listeners.forEach((listener) => listener());
    },
  };
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
  initialAction,
  prepareReadContext,
  throughCurrentPage = false,
  seed = null,
  chapterLabel: _chapterLabel = "",
  initialTab = "talk",
  navigate = null,
}: {
  bookId: string;
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
  initialAction?: "recap" | null;
  prepareReadContext?: () => Promise<ReadFrontier | null>;
  throughCurrentPage?: boolean;
  seed?: CompanionSeed | null;
  chapterLabel?: string;
  initialTab?: CompanionTab;
  navigate?: { tab: CompanionTab; personId: number | null; nonce: number } | null;
}) {
  const localSessionRef = useRef<AskSession | null>(null);
  const retainedSession = sessionRef ?? localSessionRef;
  const owner = activeOfflineOwner();
  const pass = String(readingPass);
  // Advancing the read boundary never discards the discussion. Rewinding or resetting
  // creates a new in-memory session so late results cannot reveal a later boundary.
  if (
    !retainedSession.current ||
    retainedSession.current.bookId !== bookId ||
    retainedSession.current.owner !== owner ||
    retainedSession.current.readingPass !== pass ||
    retainedSession.current.bookmark > bookmark
  ) {
    retainedSession.current = createAskSession(bookId, bookmark, owner, pass);
  }
  const session = retainedSession.current;
  session.bookmark = bookmark;
  const { question, exchanges, pendingQuestion, busy, error } =
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
  const [saveError, setSaveError] = useState("");
  const [saved, setSaved] = useState<Set<string>>(new Set());
  const [peopleOpen, setPeopleOpen] = useState(false);
  const [people, setPeople] = useState<{ name: string; entity_id: number }[]>(
    [],
  );
  const [peopleBusy, setPeopleBusy] = useState(false);
  const [peopleError, setPeopleError] = useState("");
  const [aiDisclosure, setAiDisclosure] = useState(() => {
    try {
      return localStorage.getItem("litlet.pip.introduced") !== "yes";
    } catch {
      return true;
    }
  });
  const [tab, setTab] = useState<CompanionTab>(initialTab);
  const [focusPerson, setFocusPerson] = useState<number | null>(null);
  const [suggestions, setSuggestions] = useState<string[]>([]);
  useEffect(() => setTab(initialTab), [initialTab]);
  useEffect(() => {
    if (!navigate) return;
    setTab(navigate.tab);
    setFocusPerson(navigate.personId);
  }, [navigate?.nonce]);
  const modal = !embedded || compact;
  const isCurrent = () =>
    retainedSession.current === session && activeOfflineOwner() === owner;

  useEffect(() => {
    inputRef.current?.focus({ preventScroll: true });
    const keydown = (event: KeyboardEvent) => {
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
    else
      node.scrollTop =
        node.querySelector<HTMLElement>("[data-latest-exchange]")?.offsetTop ??
        0;
  }, [exchanges.length, busy]);
  useEffect(() => {
    setPeopleOpen(false);
    setPeople([]);
    setPeopleError("");
    setPeopleBusy(false);
    setSaveError("");
    setConfirmClear(false);
  }, [bookId, owner, pass, bookmark]);

  const send = async (
    value = session.getSnapshot().question.trim(),
    bookScope = false,
  ) => {
    if (!isCurrent() || value.length < 2 || session.getSnapshot().busy) return;
    const requestToken = session.getSnapshot().requestToken + 1;
    session.update({
      question: value,
      busy: true,
      error: "",
      pendingQuestion: value,
      requestToken,
    });
    const isCurrentRequest = () =>
      isCurrent() && session.getSnapshot().requestToken === requestToken;
    const scopeSelection = bookScope ? null : selection;
    const selectionKey = scopeSelection
      ? `${scopeSelection.anchor.cfi}:${scopeSelection.text}`
      : undefined;
    try {
      let result: AskAnswer;
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
        const history = session
          .getSnapshot()
          .exchanges.filter(
            (exchange) =>
              !exchange.passage &&
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
        result = readPoint
          ? await api.askBook(bookId, value, requestBookmark, history, readPoint)
          : await api.askBook(bookId, value, requestBookmark, history);
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
      session.update({
        exchanges: [
          ...session.getSnapshot().exchanges,
          {
            id: crypto.randomUUID(),
            question: value,
            answer: result,
            passage: !!scopeSelection,
            selectionKey,
          },
        ].slice(-40),
        question: "",
        pendingQuestion: null,
      });
      setPeopleOpen(false);
    } catch (reason) {
      if (isCurrentRequest()) {
        const message =
          reason instanceof ApiError && reason.status === 413
            ? REQUEST_TOO_LARGE_MESSAGE
            : reason instanceof ApiError && reason.status === 429
            ? /cost ceiling/i.test(reason.message)
              ? BOOK_USAGE_LIMIT_MESSAGE
              : "Mothli needs a short break. Try again shortly."
            : reason instanceof ApiError && reason.status === 502 && /cleared against (your read pages|the selected passage)/i.test(reason.message)
            ? scopeSelection
              ? "I couldn’t verify that from the selected passage. Try selecting a little more context."
              : "I couldn’t verify that against your read pages. Try naming the person or moment you mean."
            : reason instanceof ApiError && reason.status === 409
            ? "Your reading position changed. Please try again from this page."
            : "I couldn’t get a reply. Try again?";
        session.update({ error: message });
      }
    } finally {
      if (isCurrentRequest())
        session.update({ busy: false, pendingQuestion: null });
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
    } catch {
      if (currentPeople()) setPeopleError("Couldn’t find the names. Try again?");
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
  useEffect(() => {
    if (!compact || !window.visualViewport) return;
    const viewport = window.visualViewport;
    const resize = () => {
      rootRef.current?.style.setProperty(
        "--mothli-viewport-height",
        `${viewport.height}px`,
      );
      rootRef.current?.style.setProperty(
        "--mothli-viewport-top",
        `${viewport.offsetTop}px`,
      );
    };
    resize();
    viewport.addEventListener("resize", resize);
    viewport.addEventListener("scroll", resize);
    return () => {
      viewport.removeEventListener("resize", resize);
      viewport.removeEventListener("scroll", resize);
    };
  }, [compact]);
  // Mothli speaks first: a recap already in hand, or a chapter closeout fetched once on open.
  const seededRef = useRef<string | null>(null);
  useEffect(() => {
    if (!seed || seededRef.current === seed.id) return;
    seededRef.current = seed.id;
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
    const isCurrentRequest = () => isCurrent() && session.getSnapshot().requestToken === requestToken;
    void api
      .chapterCloseout(bookId, seed.chapter)
      .then((result) => {
        if (!isCurrentRequest()) return;
        if (result.as_of_chapter > bookmark || result.citations.some((citation) => citation.ordinal > bookmark))
          throw new Error("unverified");
        session.update({
          exchanges: [
            ...session.getSnapshot().exchanges,
            { id: crypto.randomUUID(), question: seed.label, answer: result, fromMothli: true },
          ].slice(-40),
        });
      })
      .catch((reason: unknown) => {
        if (!isCurrentRequest()) return;
        session.update({
          error:
            reason instanceof ApiError && reason.status === 409
              ? "I’m still taking notes on that chapter. Ask me again in a minute."
              : reason instanceof ApiError && reason.status === 429
                ? BOOK_USAGE_LIMIT_MESSAGE
                : "I couldn’t gather my thoughts on that chapter. Try again?",
        });
      })
      .finally(() => {
        if (isCurrentRequest()) session.update({ busy: false });
      });
  }, [seed?.id]);

  // Suggestions are only offered when their answers have already cleared review server-side.
  useEffect(() => {
    let live = true;
    setSuggestions([]);
    if (bookmark < 1) return;
    Promise.resolve()
      .then(() => api.preparedQuestions(bookId, bookmark))
      .then((value) => {
        if (live && value && value.as_of_chapter <= bookmark) setSuggestions((value.questions ?? []).slice(0, 3));
      })
      .catch(() => {});
    return () => {
      live = false;
    };
  }, [bookId, bookmark]);

  const askAbout = (prompt: string) => {
    setTab("talk");
    onClearSelection?.();
    void send(prompt, true);
    inputRef.current?.focus({ preventScroll: true });
  };

  const visibleExchanges = exchanges.filter(
    (exchange) => exchange.passage || exchange.answer.as_of_chapter <= bookmark,
  );
  const mood: MothMood = busy ? "thinking" : error ? "sleepy" : !visibleExchanges.length ? "curious" : "idle";
  const starters = selection
    ? ["What’s going on here?", "Why does this matter?"]
    : bookmark > 0
      ? ["What just happened?", "What have I missed?"]
      : [];
  const chips = (visibleExchanges.length ? [] : starters).concat(suggestions).slice(0, 3);

  return (
    <div
      className={`mothli-panel mothli-conversation lp-companion${embedded ? " mothli-conversation--embedded" : " mothli-conversation--standalone"}${compact ? " mothli-conversation--compact" : ""}`}
      role={modal ? "dialog" : "complementary"}
      aria-modal={modal || undefined}
      aria-labelledby={titleId}
      ref={rootRef}
      data-tab={tab}
    >
      <header className="lp-companion-head">
        <div className="lp-companion-id">
          <span className="lp-moth-ring"><Moth mood={mood} size={42} glow={false} /></span>
          <div>
            <h2 id={titleId} className="lp-voice">Mothli</h2>
            <span>{busy ? "Thinking it over…" : selection ? "Talking about the passage you picked" : "Knows the story up to here"}</span>
          </div>
        </div>
        <div className="lp-companion-head-actions">
          <Button
            variant="ghost"
            className="mothli-menu"
            aria-label="Conversation options"
            title="Conversation options"
            onClick={() => setConfirmClear(!confirmClear)}
          >
            <Icon name="more" size={17} />
          </Button>
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
      {tab === "people" ? (
        <div className="lp-companion-body">
          <PeoplePanel
            bookId={bookId}
            bookmark={bookmark}
            focusId={focusPerson}
            onAsk={(name) => askAbout(`Remind me who ${name} is.`)}
          />
        </div>
      ) : tab === "story" ? (
        <div className="lp-companion-body">
          <StoryPanel
            bookId={bookId}
            bookmark={bookmark}
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
                <span>Start a fresh chat? Saved replies stay in Notebook.</span>
                <Button
                  variant="ghost"
                  onClick={() => {
                    session.update({
                      question: "",
                      exchanges: [],
                      pendingQuestion: null,
                      busy: false,
                      error: "",
                      requestToken: session.getSnapshot().requestToken + 1,
                    });
                    session.scrollTop = null;
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
            {!visibleExchanges.length && !busy && (
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
                    I’m an AI. I only use the pages you’ve already read, so I can’t spoil what’s ahead.{" "}
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
                aria-label={exchange.fromMothli ? exchange.question : `Question ${index + 1}`}
                data-latest-exchange={index === visibleExchanges.length - 1 || undefined}
                key={exchange.id}
              >
                {exchange.fromMothli ? (
                  <p className="lp-moment-label">{exchange.question}</p>
                ) : (
                  <p className="mothli-question lp-you">{exchange.question}</p>
                )}
                <div className="mothli-reply lp-reply">
                  <div className="lp-reply-body">
                    <p>{readableAnswer(exchange.answer)}</p>
                    {!exchange.answer.insufficient_evidence && exchange.answer.answer_type !== "conversation" && (
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
                        {onSaveReply && (
                          <Button
                            variant="ghost"
                            className="mothli-save lp-save"
                            icon={saved.has(exchange.id) ? "check" : "bookmark"}
                            aria-label={saved.has(exchange.id) ? "Saved to Notebook" : "Save reply to Notebook"}
                            disabled={saved.has(exchange.id)}
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
                              } catch {
                                setSaveError("Couldn’t save that reply. Tap Save to try again.");
                              }
                            }}
                          >
                            {saved.has(exchange.id) ? "Saved" : "Save"}
                          </Button>
                        )}
                      </div>
                    )}
                  </div>
                </div>
              </section>
            ))}
            {busy && (
              <section className="mothli-exchange lp-exchange" aria-label="Sending message">
                {pendingQuestion && <p className="mothli-question lp-you">{pendingQuestion}</p>}
                <div className="lp-thinking">
                  <span className="lp-thinking-dots" aria-hidden="true">
                    <i />
                    <i />
                    <i />
                  </span>
                  <button
                    className="mothli-cancel"
                    type="button"
                    onClick={() =>
                      session.update({
                        busy: false,
                        pendingQuestion: null,
                        requestToken: session.getSnapshot().requestToken + 1,
                      })
                    }
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
                      {error !== BOOK_USAGE_LIMIT_MESSAGE && error !== REQUEST_TOO_LARGE_MESSAGE && (
                        <button type="button" onClick={() => void send()}>
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
                placeholder={selection ? "Ask about this passage…" : "Ask Mothli anything about your book…"}
                value={question}
                disabled={busy}
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
