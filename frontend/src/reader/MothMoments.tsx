/** Mothli in the corner of the page. By default it's a small moth. At natural moments (a finished
 * chapter, a return after a break) it says something short in a speech bubble. Tapping the moth
 * opens the full chat. It never covers the text and never blocks reading. */
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { api, type CatchMeUp } from "../api";
import { Icon } from "../ui/Icon";
import { Moth, type MothMood } from "../ui/Moth";
import { PersonAvatar } from "./StoryMemory";
import "./moth-corner.css";

export type CornerNote = {
  id: string;
  text: string;
  people?: { entity_id: number; name: string }[];
  action?: { label: string; run: () => void };
};

export function MothCorner({
  note,
  welcome,
  disabled,
  mood = "idle",
  onOpen,
  onDismiss,
  onPerson,
}: {
  note: CornerNote | null;
  welcome?: ReactNode;
  disabled?: boolean;
  mood?: MothMood;
  onOpen: () => void;
  onDismiss: () => void;
  onPerson: (id: number) => void;
}) {
  const speaking = !!welcome || !!note;
  return (
    <div className="mc">
      {welcome}
      {!welcome && note && (
        <div className="mc-bubble" role="status" key={note.id}>
          <p>{note.text}</p>
          {note.people && note.people.length > 0 && (
            <div className="mc-people">
              {note.people.slice(0, 4).map((person) => (
                <button key={person.entity_id} type="button" onClick={() => onPerson(person.entity_id)}>
                  <PersonAvatar id={person.entity_id} name={person.name} size={28} />
                  {person.name.split(/\s+/)[0]}
                </button>
              ))}
            </div>
          )}
          <div className="mc-actions">
            {note.action && (
              <button type="button" className="mc-primary" onClick={note.action.run}>
                {note.action.label}
              </button>
            )}
            <button type="button" className="mc-quiet" onClick={onDismiss}>
              {note.action ? "Later" : "Got it"}
            </button>
          </div>
        </div>
      )}
      <button
        type="button"
        className="mc-moth"
        aria-label="Talk to Mothli"
        title="Talk to Mothli"
        disabled={disabled}
        onClick={onOpen}
      >
        <Moth mood={speaking ? "curious" : mood} size={54} glow={false} />
      </button>
    </div>
  );
}

export function formatAway(ms: number) {
  const hours = ms / 3_600_000;
  if (hours < 20) return "a few hours";
  const days = Math.round(hours / 24);
  if (days < 2) return "a day";
  if (days < 13) return `${days} days`;
  const weeks = Math.round(days / 7);
  if (weeks < 8) return `${weeks} weeks`;
  const months = Math.round(days / 30);
  if (months < 18) return `${months} months`;
  return "a long while";
}

/** The "where we left off" line: the END of a story-so-far recap is the most recent part. */
function preview(text: string, fromEnd: boolean, limit = 220) {
  const clean = text.replace(/^\[stub recap\]\s*-?\s*/i, "").replace(/\s+/g, " ").trim();
  if (clean.length <= limit) return clean;
  if (fromEnd) {
    const sentences = clean.match(/[^.!?]+[.!?]+["”’)]*/g) ?? [clean];
    let out = "";
    for (let index = sentences.length - 1; index >= 0; index -= 1) {
      const next = `${sentences[index].trim()} ${out}`.trim();
      if (next.length > limit && out) break;
      out = next;
    }
    return out.length > limit ? `…${out.slice(-limit).trimStart()}` : out;
  }
  const cut = clean.slice(0, limit);
  const stop = Math.max(cut.lastIndexOf(". "), cut.lastIndexOf("! "), cut.lastIndexOf("? "));
  return (stop > 80 ? cut.slice(0, stop + 1) : `${cut.replace(/\s+\S*$/, "").trimEnd()}…`).trim();
}

/** The welcome-back moment, said in Mothli's speech bubble. */
export function WelcomeBackCard({
  bookId,
  bookmark,
  awayMs,
  onCatchUp,
  onPerson,
  onDismiss,
}: {
  bookId: string;
  bookmark: number;
  awayMs: number;
  chapterTitle?: string;
  onCatchUp: (recap: CatchMeUp | null) => void;
  onPerson: (name: string) => void;
  onDismiss: () => void;
}) {
  const [recap, setRecap] = useState<CatchMeUp | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "none">(bookmark > 0 ? "loading" : "none");

  useEffect(() => {
    if (bookmark < 1) return;
    let live = true;
    Promise.resolve()
      .then(() => api.catchMeUp(bookId, bookmark))
      .then((value) => {
        if (!live) return;
        if (value && value.as_of_chapter <= bookmark && (value.recap || value.now)) {
          setRecap(value);
          setState("ready");
        } else setState("none");
      })
      .catch(() => live && setState("none"));
    return () => {
      live = false;
    };
  }, [bookId, bookmark]);

  const people = useMemo(() => {
    const seen = new Set<number>();
    const list: { id: number; name: string }[] = [];
    for (const member of recap?.cast ?? []) {
      if (seen.has(member.entity_id)) continue;
      seen.add(member.entity_id);
      list.push({ id: member.entity_id, name: member.name });
      if (list.length === 3) break;
    }
    return list;
  }, [recap]);

  return (
    <section className="mc-bubble welcome-back" aria-label="Welcome back">
      <button type="button" className="mc-close" aria-label="Dismiss welcome back" onClick={onDismiss}>
        <Icon name="close" size={14} />
      </button>
      <p>
        <strong>Welcome back!</strong> It’s been {formatAway(awayMs)}.{" "}
        {state === "loading"
          ? "Let me remember where we were…"
          : state === "ready" && recap
            ? `Last time: ${recap.now ? preview(recap.now, false) : preview(recap.recap ?? "", true)}`
            : bookmark > 0
              ? "Want a quick refresher before you dive back in?"
              : "Ask me anything as you go."}
      </p>
      {people.length > 0 && (
        <div className="mc-people">
          {people.map((person) => (
            <button key={person.id} type="button" onClick={() => onPerson(person.name)}>
              <PersonAvatar id={person.id} name={person.name} size={28} />
              {person.name.split(/\s+/)[0]}
            </button>
          ))}
        </div>
      )}
      <div className="mc-actions">
        <button type="button" className="mc-primary" onClick={() => onCatchUp(state === "ready" ? recap : null)}>
          Catch me up
        </button>
        <button type="button" className="mc-quiet" onClick={onDismiss}>
          Keep reading
        </button>
      </div>
    </section>
  );
}
