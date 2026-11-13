/** The small card that answers "wait, who was that?" when a name in the text is tapped.
 * Everything on it comes from bookmark-clamped routes, so it can only say what the reader has
 * already been told. No AI call is made until the reader asks Mothli for more. */
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { api, type Character, type GraphNode } from "../api";
import type { TapRect } from "./nameTaps";
import "./name-card.css";

const CARD_WIDTH = 340;
const GAP = 10;

export function NameCard({
  bookId,
  bookmark,
  person,
  calledAs,
  anchor,
  onAsk,
  onMore,
  onClose,
}: {
  bookId: string;
  bookmark: number;
  person: GraphNode;
  /** The name as it was written where the reader tapped ("Alyosha", not "Alexey"). */
  calledAs?: string;
  anchor: TapRect;
  onAsk: (name: string) => void;
  onMore: (entityId: number) => void;
  onClose: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [detail, setDetail] = useState<Character | null>(null);
  const [place, setPlace] = useState<{ left: number; top: number } | null>(null);

  useEffect(() => {
    let live = true;
    setDetail(null);
    api
      .character(bookId, person.entity_id, bookmark)
      .then((value) => live && value.as_of_chapter <= bookmark && setDetail(value))
      .catch(() => {});
    return () => {
      live = false;
    };
  }, [bookId, bookmark, person.entity_id]);

  // Below the name when there is room, above it otherwise; never off the edge of the screen.
  useLayoutEffect(() => {
    const height = ref.current?.offsetHeight ?? 200;
    const width = Math.min(CARD_WIDTH, window.innerWidth - 24);
    const left = Math.max(12, Math.min(window.innerWidth - width - 12, (anchor.left + anchor.right) / 2 - width / 2));
    const below = anchor.bottom + GAP;
    const top = below + height <= window.innerHeight - 12 ? below : Math.max(12, anchor.top - GAP - height);
    setPlace({ left, top });
  }, [anchor, detail]);

  useEffect(() => {
    ref.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    const onPointer = (event: PointerEvent) => {
      if (event.target instanceof Node && !ref.current?.contains(event.target)) onClose();
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("pointerdown", onPointer);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("pointerdown", onPointer);
    };
  }, [onClose]);

  const name = detail?.name ?? person.canonical_name;
  const aliases = (detail?.aliases ?? person.aliases ?? []).filter((alias) => alias !== name).slice(0, 3);
  const met = detail?.first_seen ?? person.revealed_at;
  const ties = (detail?.ties ?? []).slice(0, 3);
  const short = calledAs || (name.split(/\s+/)[0] ?? name);

  return (
    <div
      ref={ref}
      className="name-card"
      role="dialog"
      aria-label={name}
      tabIndex={-1}
      style={{ left: place?.left ?? 0, top: place?.top ?? 0, visibility: place ? "visible" : "hidden" }}
    >
      <h2 className="name-card-name">{name}</h2>
      <p className="name-card-met">
        {aliases.length > 0 && <>Also called {aliases.join(", ")}. </>}
        You met them in chapter {met}.
      </p>
      {ties.length > 0 && (
        <ul className="name-card-ties" aria-label={`People connected to ${short}`}>
          {ties.map((tie) => (
            <li key={`${tie.entity_id}-${tie.label}`}>
              <span>{tie.name}</span>
              <span>{tie.label}</span>
            </li>
          ))}
        </ul>
      )}
      <div className="name-card-actions">
        <button type="button" className="name-card-ask" onClick={() => onAsk(name)}>
          {/\s/.test(short) ? "Ask Mothli" : `Ask Mothli about ${short}`}
        </button>
        <button type="button" onClick={() => onMore(person.entity_id)}>
          More
        </button>
      </div>
    </div>
  );
}
