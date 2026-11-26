/** The front page's shelf: a few classics, and a button that drops more books on it, with
 * Mothli's note in the margin changing as the shelf fills. Nothing is uploaded: it is a drawing. */
import { useState } from "react";
import { Written } from "./Written";

const CLASSICS = [
  { title: "Pride and Prejudice", tone: "oxblood", size: "tall" },
  { title: "Jane Eyre", tone: "ink", size: "mid" },
  { title: "Moby-Dick", tone: "sea", size: "tall" },
  { title: "Middlemarch", tone: "gold", size: "short" },
  { title: "Frankenstein", tone: "moss", size: "mid" },
] as const;

const YOURS = [
  { title: "Your book", tone: "paper" },
  { title: "Another one", tone: "plum" },
  { title: "And one more", tone: "ochre" },
] as const;

const NOTES = [
  "Plenty of room for yours.",
  "Ooh, a new one! What’s it about?",
  "You have excellent taste. I can already tell.",
  "Okay, this shelf is perfect. I live here now.",
];

export function ShelfDemo() {
  const [added, setAdded] = useState(0);
  const full = added === YOURS.length;
  return (
    <div className="lp-shelf-wrap">
      <div className="lp-shelf-scene">
        <ul className="lp-shelf" aria-label="A shelf of books">
          {CLASSICS.map((book) => (
            <li key={book.title} className={`lp-spine lp-spine--${book.tone} lp-spine--${book.size}`}>
              <span>{book.title}</span>
            </li>
          ))}
          {YOURS.slice(0, added).map((book) => (
            <li key={book.title} className={`lp-spine lp-spine--${book.tone} lp-spine--mid lp-spine--dropped`}>
              <span>{book.title}</span>
            </li>
          ))}
        </ul>
        <span className="lp-shelf-board" aria-hidden="true" />
        <button
          type="button"
          className="lp-pill"
          onClick={() => setAdded(full ? 0 : added + 1)}
        >
          {full ? "Tidy the shelf" : "Drop a book on the shelf"}
        </button>
      </div>
      <div className="lp-margin lp-shelf-note" aria-live="polite">
        <span className="lp-perch lp-perch--shelf" data-perch="shelf" aria-hidden="true" />
        <Written key={added} text={NOTES[added]} writing={added > 0} />
      </div>
    </div>
  );
}
