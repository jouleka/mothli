/** The first time a book with nothing read in it opens: has the reader already started it
 * somewhere else, and if so, which chapter are they on? One answer, in one place.
 *
 * Picking a chapter counts every chapter before it as read by the same "I've read up to here"
 * the jump question uses (the read map's own route and checks), then opens the book there.
 * Nothing about the picked chapter itself counts, and the line under the picker says so in
 * words before anything is pressed. "I'm just starting" and Escape both close it. It never
 * blocks the page: it is a non-modal dialog, so the book behind it stays usable, and focus
 * goes back to the book when it closes. */
import { useEffect, useId, useRef, useState } from "react";
import { Button } from "../ui/Button";
import { chaptersUpTo } from "./JumpNote";

export type StartingChapter = {
  /** The piece's 0-based index in the manifest. */
  index: number;
  /** The book's own chapter number. */
  number: number;
  /** How many of the book's chapters come before it. */
  before: number;
};

const capital = (word: string) => `${word[0]?.toUpperCase() ?? ""}${word.slice(1)}`;

/** What picking ``choice`` will count as read, in words, before anything is pressed. */
export function startingLine(choice: StartingChapter, unit: string): string {
  if (choice.before === 0) return `Nothing counts as read yet. We’ll start at ${unit} ${choice.number}.`;
  return `${capital(chaptersUpTo(choice.before, unit))} will count as read. We’ll pick up at the start of ${unit} ${choice.number}.`;
}

export function StartingPlace({
  chapters,
  unit = "chapter",
  onChoose,
  onStart,
}: {
  chapters: StartingChapter[];
  unit?: string;
  /** Count the chapters before this one as read and open the book there. */
  onChoose: (choice: StartingChapter) => Promise<void>;
  /** "I'm just starting": nothing is counted, and the question is not asked again. */
  onStart: () => void;
}) {
  const rootRef = useRef<HTMLDivElement>(null);
  const pickerRef = useRef<HTMLSelectElement>(null);
  const onStartRef = useRef(onStart);
  onStartRef.current = onStart;
  const titleId = useId();
  const descriptionId = useId();
  const pickerId = useId();
  const [picked, setPicked] = useState<StartingChapter | null>(null);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const busyRef = useRef(busy);
  busyRef.current = busy;

  useEffect(() => {
    const root = rootRef.current;
    const before = document.activeElement as HTMLElement | null;
    pickerRef.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || busyRef.current) return;
      if (!root?.contains(document.activeElement)) return;
      event.preventDefault();
      onStartRef.current();
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      // Focus went back to the page while this was open: leave it there.
      const active = document.activeElement;
      if (active && active !== document.body && !root?.contains(active)) return;
      if (before && before !== document.body && document.contains(before)) before.focus();
      else document.getElementById("main")?.focus();
    };
  }, []);

  const choose = async () => {
    if (!picked || busy) return;
    setBusy(true);
    setFailed(false);
    try {
      await onChoose(picked);
    } catch {
      setFailed(true);
      setBusy(false);
    }
  };

  return (
    <div
      className="start-over-note starting-place"
      role="dialog"
      aria-modal="false"
      aria-labelledby={titleId}
      aria-describedby={descriptionId}
      ref={rootRef}
    >
      <div className="confirmation-copy">
        <strong id={titleId}>Already partway through?</strong>
        <p id={descriptionId}>
          If you’ve started this book somewhere else, tell me which {unit} you’re on. I’ll count the
          ones before it as read, so we can talk about them together.
        </p>
        <label className="starting-place-pick" htmlFor={pickerId}>
          <span>I’m on</span>
          <select
            id={pickerId}
            ref={pickerRef}
            value={picked?.index ?? ""}
            disabled={busy}
            onChange={(event) => {
              const index = Number(event.currentTarget.value);
              setPicked(chapters.find((chapter) => chapter.index === index) ?? null);
            }}
          >
            <option value="" disabled>
              pick a {unit}
            </option>
            {chapters.map((chapter) => (
              <option key={chapter.index} value={chapter.index}>
                {capital(unit)} {chapter.number}
              </option>
            ))}
          </select>
        </label>
        <p className="starting-place-line" aria-live="polite">
          {picked ? startingLine(picked, unit) : ""}
        </p>
        {failed && <span role="alert">That didn’t go through. Try once more.</span>}
      </div>
      <div className="confirmation-actions start-over-actions">
        <Button disabled={busy} onClick={onStart}>
          I’m just starting
        </Button>
        <Button variant="primary" disabled={!picked || busy} onClick={() => void choose()}>
          {busy ? "one moment…" : picked ? `take me to ${unit} ${picked.number}` : "take me there"}
        </Button>
      </div>
    </div>
  );
}
