/** Mothli on an errand: while a book is being fetched or prepared, the moth flies with it and
 * says, in pencil, what it's up to. The lines follow the real steps (fetching, unpacking the
 * chapters, shelving) at a steady pace and the last one waits, so nothing ever looks stuck.
 * A screen reader hears the first line once; the rest are for the eyes. */
import { useEffect, useState } from "react";
import "./errand.css";

export type ErrandKind = "library" | "upload";

const LINES: Record<ErrandKind, (title: string | null) => string[]> = {
  library: (title) => [
    title ? `Off to the library for ${title}…` : "Off to the library for it…",
    "Blowing the dust off the cover…",
    "Unpacking the chapters…",
    "Finding it a spot on your shelf…",
    "Nearly there. Good books take a moment.",
  ],
  upload: (title) => [
    title ? `Got it! Opening ${title}…` : "Got it! Opening the cover…",
    "Unpacking the chapters…",
    "Finding it a spot on your shelf…",
    "Nearly there. Good books take a moment.",
  ],
};

const STEP_MS = 1900;

export function Errand({ kind, title = null, className = "", announce = true }: {
  kind: ErrandKind;
  title?: string | null;
  className?: string;
  /** False where the page already says it's adding a book (the shelf's own status line). */
  announce?: boolean;
}) {
  const lines = LINES[kind](title);
  const [step, setStep] = useState(0);
  useEffect(() => {
    if (step >= lines.length - 1) return;
    const timer = window.setTimeout(() => setStep((at) => at + 1), STEP_MS);
    return () => window.clearTimeout(timer);
  }, [step, lines.length]);

  return (
    <div className={`errand ${className}`}>
      {announce && <span className="sr-only" role="status">{lines[0]}</span>}
      <svg className="errand-flight" viewBox="0 0 120 64" aria-hidden="true" focusable="false">
        <path className="errand-trail" d="M4 52c14-10 24 6 38-2s18-18 32-12" pathLength={1} />
        <g className="errand-moth">
          <g className="errand-book">
            <path d="M84 40h18v13H84z" />
            <path d="M93 40v13" />
          </g>
          <path className="errand-string" d="M93 34v6" />
          <g className="errand-wing errand-wing--left">
            <path d="M93 22c-5-10-16-14-21-10-2 8 5 16 21 16" />
            <path d="M93 28c-6 0-12 4-11 8 2 5 8 3 11-3" />
          </g>
          <g className="errand-wing errand-wing--right">
            <path d="M93 22c5-10 16-14 21-10 2 8-5 16-21 16" />
            <path d="M93 28c6 0 12 4 11 8-2 5-8 3-11-3" />
          </g>
          <path d="M93 18v18M93 18c-1-4-3-6-6-8M93 18c1-4 3-6 6-8" />
        </g>
      </svg>
      <p className="errand-line m-hand" key={step} aria-hidden="true">{lines[step]}</p>
    </div>
  );
}
