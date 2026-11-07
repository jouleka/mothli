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
import { api, ApiError, type AskAnswer, type AskCitation } from "../api";
import { Button } from "../ui/Button";
import { Icon } from "../ui/Icon";
import { activeOfflineOwner } from "../pwa/offline";
import { keepTabFocusInside } from "./focusTrap";
import type { ReaderSelection } from "./ReaderMarksPanel";
import "./pip-conversation.css";

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
};
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
  "Pip is paused for this book: its usage limit has been reached.";
const REQUEST_TOO_LARGE_MESSAGE = "That’s a lot of pages at once. Pick a passage and we can start there.";
const STORAGE_PREFIX = "litlet.pip.conversation:";
function storageKey(bookId: string, owner: string, pass: string) {
  return `${STORAGE_PREFIX}${encodeURIComponent(owner)}:${encodeURIComponent(bookId)}:${encodeURIComponent(pass)}`;
}
function readableAnswer(answer: AskAnswer): string {
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
  return answer.answer_type === "recap"
    ? (answer.recap ?? "")
    : answer.claims.map((claim) => claim.text).join("\n\n");
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

export function PipMark({ className = "" }: { className?: string }) {
  return (
    <span className={`pip-mark ${className}`} aria-hidden="true">
      <span />
      <i />
    </span>
  );
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
          insufficient_evidence: selected.insufficient_evidence,
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
        const history = session
          .getSnapshot()
          .exchanges.filter(
            (exchange) =>
              !exchange.passage &&
              exchange.answer.as_of_chapter <= bookmark &&
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
        result = await api.askBook(bookId, value, bookmark, history);
        if (
          result.as_of_chapter > bookmark ||
          result.citations.some((citation) => citation.ordinal > bookmark)
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
              : "Pip needs a short break. Try again shortly."
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
    setPeopleOpen(true);
    setPeopleBusy(true);
    setPeopleError("");
    try {
      const recap = await api.catchMeUp(bookId, bookmark);
      if (isCurrent() && recap.as_of_chapter <= bookmark) setPeople(recap.cast);
    } catch {
      if (isCurrent()) setPeopleError("Couldn’t find the names. Try again?");
    } finally {
      if (isCurrent()) setPeopleBusy(false);
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
        "--pip-viewport-height",
        `${viewport.height}px`,
      );
      rootRef.current?.style.setProperty(
        "--pip-viewport-top",
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
  const visibleExchanges = exchanges.filter(
    (exchange) => exchange.passage || exchange.answer.as_of_chapter <= bookmark,
  );
  return (
    <div
      className={`pip-panel pip-conversation${embedded ? " pip-conversation--embedded" : " pip-conversation--standalone"}${compact ? " pip-conversation--compact" : ""}`}
      role={modal ? "dialog" : "complementary"}
      aria-modal={modal || undefined}
      aria-labelledby={titleId}
      ref={rootRef}
    >
      <header className="pip-header">
        <PipMark />
        <div>
          <h2 id={titleId}>Pip</h2>
          <span>{selection ? "This passage" : "Your reading companion"}</span>
        </div>
        <Button
          variant="ghost"
          className="pip-menu"
          aria-label="Conversation options"
          title="Conversation options"
          onClick={() => setConfirmClear(!confirmClear)}
        >
          <Icon name="more" size={17} />
        </Button>
        <Button
          variant="ghost"
          className="pip-close"
          aria-label="Back to reading"
          title="Back to reading"
          onClick={onClose}
        >
          <Icon name={compact ? "arrowLeft" : "close"} size={18} />
          {compact && <span>Read</span>}
        </Button>
      </header>
      <div
        className="pip-thread"
        ref={conversationRef}
        onScroll={(event) => {
          session.scrollTop = event.currentTarget.scrollTop;
        }}
        role="region"
        aria-label="Book conversation"
      >
        {confirmClear && (
          <div className="pip-clear">
            <span>Clear this chat?</span>
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
        {aiDisclosure && (
          <div className="pip-introduction">
            <span>Pip is your AI reading companion.</span>
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
              <Icon name="close" size={14} />
            </button>
          </div>
        )}
        {!visibleExchanges.length && !busy && (
          <div className="pip-welcome">
            <PipMark />
            <p>What’s on your mind?</p>
          </div>
        )}
        {visibleExchanges.map((exchange, index) => (
          <section
            className="pip-exchange"
            aria-label={`Question ${index + 1}`}
            data-latest-exchange={
              index === visibleExchanges.length - 1 || undefined
            }
            key={exchange.id}
          >
            <p className="pip-question">{exchange.question}</p>
            <div className="pip-reply">
              <p>{readableAnswer(exchange.answer)}</p>
              {!exchange.answer.insufficient_evidence && (
                <div className="pip-reply-actions">
                  {exchange.answer.citations.length > 0 && (
                    <details className="pip-sources">
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
                      className="pip-save"
                      icon={saved.has(exchange.id) ? "check" : "bookmark"}
                      aria-label={
                        saved.has(exchange.id)
                          ? "Saved to Notebook"
                          : "Save reply to Notebook"
                      }
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
                          setSaveError(
                            "Couldn’t save that reply. Tap Save to try again.",
                          );
                        }
                      }}
                    >
                      {saved.has(exchange.id) ? "Saved" : "Save"}
                    </Button>
                  )}
                </div>
              )}
            </div>
          </section>
        ))}
        {busy && (
          <section className="pip-exchange" aria-label="Sending message">
            <p className="pip-question">{pendingQuestion}</p>
            <span className="pip-thinking" aria-hidden="true">
              •••
            </span>
            <button
              className="pip-cancel"
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
          </section>
        )}
        {peopleOpen && (
          <div className="pip-people">
            <div>
              <span>Who was it?</span>
              <button
                type="button"
                aria-label="Close people"
                onClick={() => setPeopleOpen(false)}
              >
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
              people.map((person) => (
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
                  <span className="pip-person-monogram" aria-hidden="true">
                    {person.name.trim().charAt(0)}
                  </span>
                  {person.name}
                </Button>
              ))
            ) : (
              <p>No names yet. Select a name in your book.</p>
            )}
          </div>
        )}
      </div>
      <div className="pip-bottom">
        <div className="pip-shortcuts" aria-label="Conversation shortcuts">
          <Button
            variant="ghost"
            icon="book"
            disabled={busy || !!selection}
            onClick={() => void send("Catch me up")}
          >
            Catch me up
          </Button>
          <Button
            variant="ghost"
            icon="search"
            disabled={busy}
            onClick={() => void openPeople()}
          >
            Who was that?
          </Button>
        </div>
        {selection && (
          <div className="pip-selection">
            <q>{selection.text}</q>
            {onClearSelection && (
              <button
                type="button"
                aria-label="Remove passage context"
                disabled={busy}
                onClick={onClearSelection}
              >
                <Icon name="close" size={14} />
              </button>
            )}
          </div>
        )}
        <div className="pip-status" role="status" aria-live="polite">
          {saveError ||
            (busy ? (
              <span className="sr-only">Pip is checking your pages.</span>
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
        <form className="pip-composer" onSubmit={submit}>
          <label htmlFor={`${titleId}-message`} className="sr-only">
            Message Pip
          </label>
          <textarea
            id={`${titleId}-message`}
            ref={inputRef}
            rows={2}
            maxLength={500}
            placeholder="What’s on your mind?"
            value={question}
            disabled={busy}
            onChange={(event) =>
              session.update({ question: event.currentTarget.value })
            }
            onKeyDown={(event) => {
              if (
                event.key === "Enter" &&
                !event.shiftKey &&
                !event.nativeEvent.isComposing
              ) {
                event.preventDefault();
                void send();
              }
            }}
          />
          <div>
            <span title="Pip only uses the pages you’ve read">
              <Icon name="shield" size={12} />
              <span>
                {selection ? "This passage only" : "Only your read pages"}
              </span>
            </span>
            <Button
              type="submit"
              variant="primary"
              aria-label="Send message"
              className="pip-send"
              disabled={busy || question.trim().length < 2}
            >
              <Icon name="arrowRight" size={18} />
            </Button>
          </div>
        </form>
      </div>
    </div>
  );
}
