/** The front page's passage: mark a line and Mothli answers in the margin beside it. The first
 * draw has the first line marked and its note written (the same with or without JavaScript). */
import { useState } from "react";
import { Moth } from "../../ui/Moth";
import { OPENING } from "./pride";
import { Written } from "./Written";

const MARKS = OPENING.flatMap((part) => ("mark" in part ? [part] : []));

export function MarkALine() {
  const [marked, setMarked] = useState(0);
  const [take, setTake] = useState(0);
  let index = -1;
  const choose = (which: number) => {
    setMarked(which);
    setTake((count) => count + 1);
  };
  return (
    <div className="lp-mark-wrap">
      <div className="lp-mark-text">
        <p className="lp-mark-chapter">Chapter I</p>
        <p className="lp-mark-passage">
          {OPENING.map((part, at) => {
            if (!("mark" in part)) return <span key={at}>{part.text}</span>;
            index += 1;
            const which = index;
            return (
              // Not a <button>: a button can't wrap across lines, and these are a sentence's words.
              <span
                key={at}
                role="button"
                tabIndex={0}
                className="lp-mark"
                aria-pressed={which === marked}
                onClick={() => choose(which)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    choose(which);
                  }
                }}
              >
                {part.mark}
              </span>
            );
          })}
        </p>
      </div>
      <div className="lp-margin lp-mark-note" aria-live="polite">
        <span className="lp-perch lp-perch--mark" data-perch="mark" aria-hidden="true" />
        <div className="lp-mark-said" key={`note-${take}`} data-mark={marked}>
          <Moth size={30} className="lp-mark-moth" />
          <Written text={MARKS[marked].note} writing={take > 0} />
        </div>
      </div>
    </div>
  );
}
