/** Taking a book off the shelf: a plain second step that says, for the mode the reader is in,
 * what goes and what stays, with "Keep it" first. A modal sheet like the account sheet (fl-scrim /
 * fl-sheet), with the same focus handling: the page behind is inert, Escape or a tap outside keeps
 * the book, Tab stays inside, and focus goes back to where it came from (Shelf.tsx). */
import { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api, type Book } from "../api";
import { keepTabFocusInside } from "../reader/focusTrap";
import { Button } from "../ui/Button";
import { Icon } from "../ui/Icon";
import "../library/free-library.css";
import "./home-chat.css";
import "./account.css";

export const REMOVE_FAILED = "I couldn’t remove it just now. Check your connection and try again.";

/** What happens, as each mode's server does it (app/api/books.py and app/catalog/catalog.py
 * locally; app/hosted/tenant/api.py and repository.py remove_book in hosted mode, which erases
 * every row about the book's reading and keeps only what its replies cost, as a bare record), and
 * as this device does it (api.deleteBook). */
export const REMOVAL = {
  local: {
    goes: [
      "Your place in it, and your chat with Mothli about it.",
      "Mothli’s notes on its chapters, and what Mothli remembers from it.",
      "Your highlights, notes, bookmarks and saved replies, which live on this device.",
      "The copy saved for offline reading.",
    ],
    stays: [
      "The book’s file, and what Mothli learned from its pages, in Mothli’s folder on this computer.",
      "What Mothli remembers about you that isn’t about this book.",
    ],
  },
  hosted: {
    goes: [
      "The book’s file, from Mothli’s storage.",
      "Your place in it, and your highlights, notes and bookmarks.",
      "Your chat with Mothli about it, and what you said on your home page while you were reading it.",
      "Mothli’s notes on its chapters, what it learned from its pages, and what it remembers from it.",
      "The copy saved for offline reading, and the replies you saved from Mothli, on this device.",
    ],
    stays: ["What Mothli remembers about you that isn’t about this book."],
  },
};

export function RemoveBook({
  book,
  hosted,
  onRemoved,
  onClose,
}: {
  book: Book;
  hosted: boolean;
  onRemoved: () => void;
  onClose: () => void;
}) {
  const [removing, setRemoving] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const keepRef = useRef<HTMLButtonElement>(null);
  const removingRef = useRef(false);
  removingRef.current = removing;
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const titleId = useId();
  const leadId = useId();
  const told = REMOVAL[hosted ? "hosted" : "local"];

  useEffect(() => {
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const siblings = Array.from(document.body.children)
      .filter((element) => element !== rootRef.current)
      .map((element) => ({ element: element as HTMLElement, inert: (element as HTMLElement).inert }));
    for (const { element } of siblings) element.inert = true;
    // "Keep it" first: Enter or a stray tap on the first button never removes the book.
    keepRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        if (!removingRef.current) onCloseRef.current();
      } else keepTabFocusInside(rootRef.current, event);
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
      for (const { element, inert } of siblings) element.inert = inert;
    };
  }, []);

  const remove = async () => {
    setRemoving(true);
    setProblem(null);
    try {
      await api.deleteBook(book.book_id);
      onRemoved();
    } catch {
      setProblem(REMOVE_FAILED);
      setRemoving(false);
    }
  };

  return createPortal(
    <div
      className="fl-scrim"
      ref={rootRef}
      onPointerDown={(event) => event.target === event.currentTarget && !removing && onClose()}
    >
      <div
        className="fl-sheet ml-mem ml-account"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={leadId}
      >
        <button type="button" className="fl-sheet-close" aria-label="Close and keep the book" disabled={removing} onClick={onClose}>
          <Icon name="close" size={18} />
        </button>
        <div className="ml-mem-body">
          <h2 id={titleId}>Remove “{book.title}”?</h2>
          <p className="ml-account-who" id={leadId}>
            It leaves your shelf. You can add it again any time, and start from the beginning.
          </p>
          <h3 className="ml-mem-book">What goes</h3>
          <ul className="ml-account-steps">
            {told.goes.map((line) => <li key={line}>{line}</li>)}
          </ul>
          <h3 className="ml-mem-book">What stays</h3>
          <ul className="ml-account-steps">
            {told.stays.map((line) => <li key={line}>{line}</li>)}
          </ul>
          {problem && <p className="fl-error" role="alert">{problem}</p>}
          <div className="ml-pending-actions">
            <Button ref={keepRef} variant="secondary" disabled={removing} onClick={onClose}>
              Keep it
            </Button>
            <Button
              variant="danger"
              disabled={removing}
              aria-busy={removing || undefined}
              onClick={() => void remove()}
            >
              {removing ? "Removing…" : "Remove book"}
            </Button>
          </div>
        </div>
      </div>
    </div>,
    document.body,
  );
}
