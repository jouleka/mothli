import { useEffect, useId, useRef } from "react";
import { Button } from "../ui/Button";
import { keepTabFocusInside } from "./focusTrap";
import { isolateReaderDialog } from "./JumpNote";

/** Destructive-reading-state confirmation. Memory is retained; only the visible frontier rewinds. */
export function StartOverNote({
  busy,
  error,
  onConfirm,
  onFresh,
  onCancel,
}: {
  busy: boolean;
  error: string | null;
  onConfirm: () => void;
  /** With the read map: start again and have Mothli forget the book until it is read again. */
  onFresh?: () => void;
  onCancel: () => void;
}) {
  const rootRef = useRef<HTMLDivElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const triggerRef = useRef<HTMLElement | null>(
    document.activeElement as HTMLElement | null,
  );
  const busyRef = useRef(busy);
  const onCancelRef = useRef(onCancel);
  busyRef.current = busy;
  onCancelRef.current = onCancel;
  const titleId = useId();
  const descriptionId = useId();

  useEffect(() => {
    const restoreBackground = rootRef.current
      ? isolateReaderDialog(rootRef.current)
      : () => {};
    if (busyRef.current) rootRef.current?.focus();
    else cancelRef.current?.focus();

    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        if (!busyRef.current) onCancelRef.current();
        return;
      }
      if (event.key === "Tab" && busyRef.current) {
        event.preventDefault();
        rootRef.current?.focus();
        return;
      }
      keepTabFocusInside(rootRef.current, event);
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      restoreBackground();
      const trigger = triggerRef.current;
      if (trigger && document.contains(trigger)) trigger.focus();
      else document.getElementById("main")?.focus();
    };
  }, []);

  useEffect(() => {
    if (busy) rootRef.current?.focus();
    else if (rootRef.current?.contains(document.activeElement))
      cancelRef.current?.focus();
  }, [busy]);

  return (
    <div
      className="start-over-note"
      role="alertdialog"
      aria-modal="true"
      aria-labelledby={titleId}
      aria-describedby={descriptionId}
      aria-busy={busy}
      tabIndex={-1}
      ref={rootRef}
    >
      <div className="confirmation-copy">
        <strong id={titleId}>Start a new reading pass?</strong>
        {onFresh ? (
          <p id={descriptionId}>
            Reading again takes you back to the start, and Mothli keeps what
            you’ve read and said, so it can talk about the whole book as you
            go. Reading it fresh makes Mothli forget this book until you read
            it again. Your book, saved notes, and past AI costs stay intact
            either way.
          </p>
        ) : (
          <p id={descriptionId}>
            The companion will return to nothing read and reveal your saved memory
            as you read again. Your book, saved notes, and past AI costs stay
            intact.
          </p>
        )}
      </div>
      {error && <span role="alert">{error}</span>}
      <div className="confirmation-actions start-over-actions">
        <Button ref={cancelRef} disabled={busy} onClick={onCancel}>
          keep my place
        </Button>
        <Button variant={onFresh ? "primary" : "danger"} disabled={busy} onClick={onConfirm}>
          {busy ? "starting…" : onFresh ? "read again" : "start again"}
        </Button>
        {onFresh && (
          <Button variant="danger" disabled={busy} onClick={onFresh}>
            read it fresh
          </Button>
        )}
      </div>
    </div>
  );
}
