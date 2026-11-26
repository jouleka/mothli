/** The front page's open book: move the ribbon to where you've read up to, ask Mothli something,
 * and see the answer (and who's who) change with the bookmark. The first draw is the same with or
 * without JavaScript: chapter three, the first thing asked, the answer already written. */
import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import { Moth } from "../../ui/Moth";
import { ANSWERS, ASKS, CAST, STOPS, castLine, type AskId, type StopId } from "./pride";
import { Written } from "./Written";

const THINKING_MS = 650;

export function BookmarkDemo() {
  const [at, setAt] = useState<StopId>("three");
  const [ask, setAsk] = useState<AskId>("darcy");
  // How many times something has been asked here: 0 is the first draw, nothing being written.
  const [take, setTake] = useState(0);
  const [thinking, setThinking] = useState(false);
  const timer = useRef<number | undefined>(undefined);
  const track = useRef<HTMLDivElement>(null);
  const dragging = useRef(false);

  useEffect(() => () => window.clearTimeout(timer.current), []);

  const answer = (nextAt: StopId, nextAsk: AskId) => {
    setAt(nextAt);
    setAsk(nextAsk);
    setTake((count) => count + 1);
    setThinking(true);
    window.clearTimeout(timer.current);
    const still = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    timer.current = window.setTimeout(() => setThinking(false), still ? 0 : THINKING_MS);
  };

  const moveTo = (next: StopId) => {
    if (next !== at) answer(next, ask);
  };

  // Dragging along the page edge: the ribbon jumps to the nearest chapter under the finger.
  const nearest = (clientX: number): StopId | null => {
    const buttons = track.current?.querySelectorAll<HTMLElement>("[data-stop]");
    if (!buttons?.length) return null;
    let best: StopId | null = null;
    let distance = Infinity;
    buttons.forEach((button) => {
      const box = button.getBoundingClientRect();
      const gap = Math.abs(box.left + box.width / 2 - clientX);
      if (gap < distance) {
        distance = gap;
        best = button.dataset.stop as StopId;
      }
    });
    return best;
  };
  const onPointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.pointerType === "mouse" && event.button !== 0) return;
    dragging.current = true;
  };
  const onPointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!dragging.current) return;
    const next = nearest(event.clientX);
    if (next) moveTo(next);
  };
  const stopDragging = () => {
    dragging.current = false;
  };

  const stop = STOPS.find((each) => each.id === at)!;
  const asked = ASKS.find((each) => each.id === ask)!;
  const met = CAST.flatMap((entry) => {
    const line = castLine(entry, at);
    return line ? [{ name: entry.name, line }] : [];
  });

  return (
    <div className="lp-demo" data-at={at}>
      <div
        className="lp-edge"
        ref={track}
        role="group"
        aria-label="How far you’ve read"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={stopDragging}
        onPointerCancel={stopDragging}
        onPointerLeave={stopDragging}
      >
        <span className="lp-edge-line" aria-hidden="true" />
        {STOPS.map((each) => (
          <button
            key={each.id}
            type="button"
            className="lp-edge-stop"
            data-stop={each.id}
            aria-pressed={each.id === at}
            aria-label={`Read up to ${each.spoken}`}
            onClick={() => moveTo(each.id)}
          >
            {each.numeral}
          </button>
        ))}
        <span className="lp-edge-end" aria-hidden="true">LXI</span>
        <span className="lp-ribbon" aria-hidden="true">
          <span className="lp-ribbon-tail" key={`ribbon-${at}`} />
        </span>
      </div>

      <div className="lp-book">
        <div className="lp-book-page lp-book-page--left">
          <p className="lp-folio">Pride and Prejudice, by Jane Austen</p>
          <div className="lp-passage-wrap" key={`passage-${at}`}>
            <p className="lp-chapter">Chapter {stop.numeral}</p>
            <blockquote className="lp-passage">
              <p>“{stop.passage}”</p>
            </blockquote>
            <p className="lp-passage-said">{stop.said}</p>
          </div>
          <h3 className="lp-cast-title">Who’s who so far</h3>
          <ul className="lp-cast">
            {met.map((person) => (
              <li key={person.name} className="lp-cast-card">
                <strong>{person.name}</strong>
                <span key={person.line} className="lp-cast-line">{person.line}</span>
              </li>
            ))}
          </ul>
        </div>

        <div className="lp-book-page lp-book-page--right">
          <div className="lp-chat" aria-live="polite">
            <p className="lp-chat-reader" key={`asked-${take}`}>{asked.text}</p>
            <div className="lp-chat-mothli" key={`said-${take}`}>
              <Moth size={34} mood={thinking ? "thinking" : "idle"} className="lp-chat-moth" />
              {thinking ? (
                <p className="lp-chat-thinking">Mothli is thinking…</p>
              ) : (
                <Written text={ANSWERS[at][ask]} writing={take > 0} className="lp-chat-said" />
              )}
            </div>
          </div>
          <div className="lp-asks" role="group" aria-label="Ask Mothli">
            {ASKS.map((each) => (
              <button
                key={each.id}
                type="button"
                className="lp-ask"
                aria-pressed={each.id === ask}
                onClick={() => answer(at, each.id)}
              >
                {each.text}
              </button>
            ))}
          </div>
          <span className="lp-perch lp-perch--chat" data-perch="bookmark" aria-hidden="true" />
        </div>
      </div>
    </div>
  );
}
