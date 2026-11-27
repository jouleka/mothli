/** Hosted mode: the one invitation a new reader sees, about the first book on their shelf.
 *
 * A reader who has never had Mothli (`new`) and has just put a book on their shelf sees, once, a
 * small card in Mothli's voice about that book, with a button that opens the plan sheet and an
 * equal "Keep reading for free". It sits on the shelf like any other card: nothing is blocked,
 * nothing takes focus, and Escape closes it like the second button.
 *
 * "Once" is kept on this device only, for the reader signed in here (as `keptQuestion` keys its
 * questions): the card is noted as seen when it first shows, so it never comes back, whichever
 * button is pressed or none. There is no server-side place for such a flag, so another device may
 * show it once more. It costs nothing: no call is made to show it. */
import { type KeyboardEvent } from "react";
import type { Book } from "../api";
import { activeOfflineOwner } from "../pwa/offline";
import { Button } from "../ui/Button";
import { Moth } from "../ui/Moth";
import { START_TALKING, usePlanSheet } from "../ui/MothliPlan";

const PREFIX = "mothli.firstBookInvitation:";
/** Seen in this visit, where storage is unavailable. */
const memory = new Set<string>();

const keyOf = () => `${PREFIX}${encodeURIComponent(activeOfflineOwner())}`;

/** Whether the reader signed in here has already been shown the invitation. */
export function firstBookInvitationSeen(): boolean {
  const key = keyOf();
  try {
    if (localStorage.getItem(key) !== null) return true;
  } catch {
    /* No storage here: what this visit noted. */
  }
  return memory.has(key);
}

/** The invitation has been shown: never again for this reader on this device. */
export function noteFirstBookInvitationSeen(): void {
  const key = keyOf();
  try {
    localStorage.setItem(key, "1");
  } catch {
    memory.add(key);
  }
}

export const KEEP_READING_FREE = "Keep reading for free";

/** Mothli's words about the reader's first book (the title is set apart by the card). */
export const FIRST_BOOK_AFTER_TITLE =
  "I’d love to read along with you. Ask me anything about the pages you’ve read, like who’s who or what just happened, and I’ll never spoil what’s ahead. Join me on the plan whenever you like.";

export function FirstBookInvitation({ book, onDone }: { book: Book; onDone: () => void }) {
  // Closed without joining: the card goes, as with "Keep reading for free". Joining changes where
  // the reader stands, and the shelf drops the card itself.
  const { buttonRef, open, sheet } = usePlanSheet(onDone);
  const closeOnEscape = (event: KeyboardEvent) => {
    if (event.key === "Escape" && !sheet) {
      event.stopPropagation();
      onDone();
    }
  };
  return (
    <section
      className="ml-first-invite"
      aria-labelledby="ml-first-invite-words"
      onKeyDown={closeOnEscape}
    >
      <Moth mood="curious" size={52} />
      <div className="ml-first-invite-body">
        <p id="ml-first-invite-words" className="m-hand ml-first-invite-words">
          Ooh, <cite>{book.title}</cite>! {FIRST_BOOK_AFTER_TITLE}
        </p>
        <div className="ml-first-invite-actions">
          <Button ref={buttonRef} variant="secondary" aria-haspopup="dialog" onClick={open}>
            {START_TALKING}
          </Button>
          <Button variant="secondary" onClick={onDone}>
            {KEEP_READING_FREE}
          </Button>
        </div>
      </div>
      {sheet}
    </section>
  );
}
