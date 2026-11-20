/** The jump-confirmation strip (LIT-13 spoiler guard, LIT-16 a11y): an alert dialog laid over the
 * page foot when an in-book link would leap past the next chapter. It interrupts with a
 * spoiler-relevant choice, so it behaves like a real dialog: focus moves onto it (the safe choice),
 * Tab is trapped within its controls (the reader behind it must not be operable mid-decision), Escape
 * dismisses to the safe default, and focus returns to the trigger when it closes (WCAG 2.4.3). */
import { useEffect, useId, useRef } from "react";
import { Button } from "../ui/Button";
import { keepTabFocusInside } from "./focusTrap";

/** Isolate a dialog nested inside the reader without making its containing main inert. */
export function isolateReaderDialog(root: HTMLElement): () => void {
  const siblings: Array<{ element: HTMLElement; inert: boolean }> = [];
  let branch = root;
  while (branch.parentElement) {
    const parent = branch.parentElement;
    for (const element of Array.from(parent.children)) {
      if (element === branch || !(element instanceof HTMLElement)) continue;
      siblings.push({ element, inert: element.inert });
      element.inert = true;
    }
    if (parent === document.body) break;
    branch = parent;
  }
  const blockOutside = (event: Event) => {
    if (event.target instanceof Node && !root.contains(event.target)) {
      event.preventDefault();
      event.stopImmediatePropagation();
    }
  };
  document.addEventListener("pointerdown", blockOutside, true);
  document.addEventListener("click", blockOutside, true);
  return () => {
    document.removeEventListener("pointerdown", blockOutside, true);
    document.removeEventListener("click", blockOutside, true);
    for (const { element, inert } of siblings) element.inert = inert;
  };
}

/** "chapters 1 to 30", "chapters 1 and 2", "chapter 1": the chapters the reader's word would
 * count as read. */
export function chaptersUpTo(count: number, unit = "chapter"): string {
  if (count === 1) return `${unit} 1`;
  return count === 2 ? `${unit}s 1 and 2` : `${unit}s 1 to ${count}`;
}

export function JumpNote({
  chapter,
  readThrough,
  onFollow,
  onFollowRead,
  onStay,
}: {
  chapter: number;
  /** How many of the book's chapters come before this one: what "I've read up to here" counts
   * as read, named on the button so nobody has to guess whether this chapter is among them. */
  readThrough?: number;
  onFollow: () => void;
  /** With the read map: go there and tell Mothli everything before it has been read. */
  onFollowRead?: () => void;
  onStay: () => void;
}) {
  const rootRef = useRef<HTMLDivElement>(null);
  const stayRef = useRef<HTMLButtonElement>(null);
  const triggerRef = useRef<HTMLElement | null>(
    document.activeElement as HTMLElement | null,
  );
  const onStayRef = useRef(onStay);
  onStayRef.current = onStay;
  const titleId = useId();
  const descriptionId = useId();
  const before = readThrough ?? chapter - 1;
  const canSayRead = onFollowRead && before > 0;

  useEffect(() => {
    const restoreBackground = rootRef.current
      ? isolateReaderDialog(rootRef.current)
      : () => {};
    stayRef.current?.focus(); // land the keyboard on the safe choice, not the destructive one

    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onStayRef.current();
        return;
      }
      keepTabFocusInside(rootRef.current, e);
    };
    document.addEventListener("keydown", onKey);

    return () => {
      document.removeEventListener("keydown", onKey);
      restoreBackground();
      // return focus where it came from, so the reader does not lose its place (2.4.3); fall back to
      // the book if the trigger is gone
      const trigger = triggerRef.current;
      if (
        trigger &&
        document.contains(trigger) &&
        typeof trigger.focus === "function"
      )
        trigger.focus();
      else document.getElementById("main")?.focus();
    };
  }, []);

  return (
    <div
      className="jump-note"
      role="alertdialog"
      aria-modal="true"
      aria-labelledby={titleId}
      aria-describedby={descriptionId}
      ref={rootRef}
      tabIndex={-1}
    >
      <div className="confirmation-copy">
        <strong id={titleId}>Skip ahead to chapter {chapter}?</strong>
        {onFollowRead ? (
          <p id={descriptionId}>
            Mothli only knows the pages you’ve read, so it won’t know what you
            skip.{canSayRead && <> Already read {chaptersUpTo(before)}? Say so and they’ll count as read.
            Chapter {chapter} itself stays unread until you read it.</>}
          </p>
        ) : (
          <p id={descriptionId}>
            Following this link marks earlier chapters as read and makes their
            details available to your companion. Continue if you have already read
            them.
          </p>
        )}
      </div>
      <div className="confirmation-actions start-over-actions">
        <Button ref={stayRef} onClick={onStay}>
          stay
        </Button>
        <Button variant="primary" onClick={onFollow}>
          {onFollowRead ? "go there" : "follow"}
        </Button>
        {canSayRead && (
          <Button onClick={onFollowRead}>I’ve read {chaptersUpTo(before)}</Button>
        )}
      </div>
    </div>
  );
}
