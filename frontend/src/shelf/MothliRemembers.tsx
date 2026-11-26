/** What Mothli remembers about the reader: the short things it has kept from what the reader told
 * it, each with when it was said and, when the server may show them, the reader's own words. The
 * reader can forget any of them, or all of them. Opened from the home chat; a modal sheet built
 * like the book sheet of the reading history (fl-scrim / fl-sheet), with the focus handling of the
 * provider settings dialog. Nothing here decides what may be shown: the server does. */
import { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api, ApiError, type Recollection } from "../api";
import { keepTabFocusInside } from "../reader/focusTrap";
import { Button } from "../ui/Button";
import { Icon } from "../ui/Icon";
import "../library/free-library.css";
import "./home-chat.css";

const LOAD_FAILED = "Mothli couldn’t look up what it remembers just now. Try again in a moment.";
const NOT_KEPT = "Mothli doesn’t remember anything between conversations here.";
const FORGET_FAILED = "Mothli couldn’t forget that just now. Try again in a moment.";
const FORGET_ALL_FAILED = "Mothli couldn’t forget everything just now. Try again in a moment.";
const EMPTY =
  "Mothli hasn’t kept anything about you yet. When you tell it how you like to read, what you love or " +
  "can’t stand in a book, or what you’d like to read next, it remembers that here.";

/** When something was said, in plain words: "today", "yesterday", or the day itself. */
export function saidWhen(at: string, now = new Date()): string {
  const when = new Date(at);
  if (Number.isNaN(when.getTime())) return "";
  const day = (date: Date) => new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
  const days = Math.round((day(now) - day(when)) / 86_400_000);
  if (days === 0) return "You said this today";
  if (days === 1) return "You said this yesterday";
  return `You said this on ${when.toLocaleDateString("en-GB", { day: "numeric", month: "long", year: "numeric" })}`;
}

const keptBack = (count: number) =>
  count === 1
    ? "1 more is kept back until you have read those pages again."
    : `${count} more are kept back until you have read those pages again.`;

export function MothliRemembers({ onClose }: { onClose: () => void }) {
  const [items, setItems] = useState<Recollection[] | null>(null);
  const [hidden, setHidden] = useState(0);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [forgetting, setForgetting] = useState<Recollection["id"] | null>(null);
  const [sure, setSure] = useState(false);
  const [clearing, setClearing] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const allRef = useRef<HTMLButtonElement>(null);
  const keepRef = useRef<HTMLButtonElement>(null);
  const listRef = useRef<HTMLOListElement>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  // Where keyboard focus goes once the change it follows is on screen.
  const focusNext = useRef<(() => void) | null>(null);
  useEffect(() => {
    const move = focusNext.current;
    focusNext.current = null;
    move?.();
  });
  const titleId = useId();
  const confirmId = useId();
  const noteId = useId();

  // A modal: the page behind is inert, Escape closes it, Tab stays inside. The opener puts focus
  // back where it was.
  useEffect(() => {
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const siblings = Array.from(document.body.children)
      .filter((element) => element !== rootRef.current)
      .map((element) => ({ element: element as HTMLElement, inert: (element as HTMLElement).inert }));
    for (const { element } of siblings) element.inert = true;
    closeRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onCloseRef.current();
      } else keepTabFocusInside(rootRef.current, event);
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
      for (const { element, inert } of siblings) element.inert = inert;
    };
  }, []);

  useEffect(() => {
    let stale = false;
    api.recollections()
      .then((found) => {
        if (stale) return;
        setItems(found.recollections);
        setHidden(found.hidden);
      })
      .catch((reason) => {
        if (!stale) setLoadError(reason instanceof ApiError && reason.status === 404 ? NOT_KEPT : LOAD_FAILED);
      });
    return () => { stale = true; };
  }, []);

  const forget = async (item: Recollection, at: number) => {
    if (forgetting !== null || !items) return;
    setForgetting(item.id);
    setProblem(null);
    try {
      await api.forgetRecollection(item.id);
    } catch {
      setProblem(FORGET_FAILED);
      setForgetting(null);
      return;
    }
    // Focus goes to the next one's button, else the one before, else the heading.
    focusNext.current = () => {
      const buttons = listRef.current?.querySelectorAll<HTMLButtonElement>(".ml-mem-forget");
      const next = buttons?.[Math.min(at, (buttons?.length ?? 0) - 1)];
      (next ?? headingRef.current)?.focus();
    };
    setItems(items.filter((other) => other.id !== item.id));
    setForgetting(null);
  };

  const forgetAll = async () => {
    setClearing(true);
    setProblem(null);
    try {
      await api.forgetRecollections();
    } catch {
      setProblem(FORGET_ALL_FAILED);
      setClearing(false);
      return;
    }
    focusNext.current = () => headingRef.current?.focus();
    setItems([]);
    setHidden(0);
    setSure(false);
    setClearing(false);
  };

  const anything = (items?.length ?? 0) > 0 || hidden > 0;

  return createPortal(
    <div className="fl-scrim" ref={rootRef} onPointerDown={(event) => event.target === event.currentTarget && onClose()}>
      <div className="fl-sheet ml-mem" role="dialog" aria-modal="true" aria-labelledby={titleId}>
        <button ref={closeRef} type="button" className="fl-sheet-close" aria-label="Close" onClick={onClose}>
          <Icon name="close" size={18} />
        </button>
        <div className="ml-mem-body">
          <h2 id={titleId} ref={headingRef} tabIndex={-1}>What Mothli remembers</h2>
          <p className="ml-mem-intro">
            Things you’ve told Mothli about yourself, kept so it can talk with you the way a friend would.
            Forget anything you’d rather it didn’t keep.
          </p>

          {loadError && <p className="fl-error" role="alert">{loadError}</p>}
          {items === null && !loadError && <p className="ml-mem-quiet" role="status">Leafing through my notebook…</p>}
          {items !== null && items.length === 0 && <p className="ml-mem-empty">{EMPTY}</p>}

          {items !== null && items.length > 0 && (
            <ol className="ml-mem-list" ref={listRef}>
              {items.map((item, at) => (
                <li key={item.id} className={`ml-mem-item${forgetting === item.id ? " is-forgetting" : ""}`}>
                  {item.book && <p className="ml-mem-book">About {item.book.title}</p>}
                  <p className="ml-mem-text" id={`${noteId}-${item.id}`}>{item.text}</p>
                  {item.message && <blockquote className="ml-mem-said">“{item.message}”</blockquote>}
                  <div className="ml-mem-foot">
                    <span className="ml-mem-when">{saidWhen(item.said_at)}</span>
                    <button
                      type="button"
                      className="ml-hc-link ml-mem-forget"
                      aria-describedby={`${noteId}-${item.id}`}
                      disabled={forgetting !== null || clearing}
                      aria-busy={forgetting === item.id || undefined}
                      onClick={() => void forget(item, at)}
                    >
                      Forget this
                    </button>
                  </div>
                </li>
              ))}
            </ol>
          )}

          {hidden > 0 && <p className="ml-mem-quiet">{keptBack(hidden)}</p>}
          {problem && <p className="fl-error" role="alert">{problem}</p>}

          {anything && (
            <div className="ml-mem-all">
              {sure ? (
                <div className="ml-hc-confirm" role="group" aria-labelledby={confirmId}>
                  <span id={confirmId}>
                    Forget everything Mothli remembers about you? Everything on this list goes
                    {hidden > 0 ? ", and so do the ones kept back" : ""}, and nothing you’ve said so far will be
                    remembered again. This can’t be undone. Your conversations stay as they are.
                  </span>
                  <Button variant="danger" disabled={clearing} aria-busy={clearing || undefined} onClick={() => void forgetAll()}>
                    Yes, forget everything
                  </Button>
                  <button
                    ref={keepRef}
                    type="button"
                    className="ml-hc-link ml-hc-keep"
                    onClick={() => {
                      focusNext.current = () => allRef.current?.focus();
                      setSure(false);
                    }}
                  >
                    Keep them
                  </button>
                </div>
              ) : (
                <button
                  ref={allRef}
                  type="button"
                  className="ml-hc-link ml-hc-clear"
                  disabled={forgetting !== null}
                  onClick={() => {
                    focusNext.current = () => keepRef.current?.focus();
                    setSure(true);
                  }}
                >
                  Forget everything
                </button>
              )}
            </div>
          )}
          {/* The one place the app says what writes Mothli's words (the owner's choice,
              2026-11-26: a friend in its own voice, with the legal duty kept quietly here). */}
          <p className="ml-mem-fine">
            A quiet word: Mothli’s replies are written by an AI model, so now and then it can get
            something wrong.
          </p>
        </div>
      </div>
    </div>,
    document.body,
  );
}
