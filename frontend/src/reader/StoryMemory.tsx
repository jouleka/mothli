/** Mothli's memory, made visible: the people you've met and the story so far. Everything here is
 * served by bookmark-clamped routes (graph, character, notes, catch-me-up), so nothing past the
 * reader's frontier can appear. Read-only; asking about something hands off to the conversation. */
import { useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError, type ChapterNote, type Character, type EntityId, type GraphNode } from "../api";
import { Icon } from "../ui/Icon";
import { Moth } from "../ui/Moth";

const TONES = ["wing", "moss", "dusk", "rose", "sky"] as const;
/** A person's colour, always the same for them. Ids are numbers locally and UUIDs in hosted mode. */
export function toneFor(id: EntityId) {
  let value = 0;
  if (typeof id === "number") value = id;
  else for (const char of id) value = (value * 31 + char.charCodeAt(0)) % 1000003;
  return TONES[Math.abs(value * 7 + 3) % TONES.length];
}
/** Local ids count up in the order people were met, so they break a tie; hosted ids do not, and
 * the server's own order stands. */
const byId = (a: EntityId, b: EntityId) => (typeof a === "number" && typeof b === "number" ? a - b : 0);
function initials(name: string) {
  const parts = name.replace(/[^\p{L}\s'-]/gu, "").trim().split(/\s+/);
  return ((parts[0]?.[0] ?? "?") + (parts.length > 1 ? parts[parts.length - 1][0] : "")).toUpperCase();
}
export function shortName(name: string) {
  return name.split(/\s+/)[0] ?? name;
}

export function PersonAvatar({ id, name, size = 40 }: { id: EntityId; name: string; size?: number }) {
  return (
    <span
      className={`memory-avatar memory-avatar--${toneFor(id)}`}
      style={{ width: size, height: size, fontSize: size * 0.38 }}
      aria-hidden="true"
    >
      {initials(name)}
    </span>
  );
}

const TIE_GROUPS: { key: string; label: string }[] = [
  { key: "family", label: "Family" },
  { key: "love", label: "Close to their heart" },
  { key: "ally", label: "Allies and friends" },
  { key: "friend", label: "Allies and friends" },
  { key: "conflict", label: "Tension" },
  { key: "rival", label: "Tension" },
  { key: "other", label: "Also connected" },
];
function groupLabel(relType: string) {
  return TIE_GROUPS.find((group) => group.key === relType)?.label ?? "Also connected";
}

/** "absent" when the server keeps no story memory for this book (a 404: hosted mode, where none
 * is built yet), so the panel says so instead of asking the reader to try again. */
type Failure = false | true | "absent";
const failure = (reason: unknown): Failure =>
  reason instanceof ApiError && reason.status === 404 ? "absent" : true;

function MemoryState({ mood, children }: { mood: "thinking" | "sleepy" | "curious"; children: React.ReactNode }) {
  return (
    <div className="memory-state">
      <Moth mood={mood} size={64} />
      <p className="lp-voice">{children}</p>
    </div>
  );
}

const WAITING = "I’m a little behind you. I’ll catch up on the rest tomorrow.";
/** A chapter Mothli could not make notes on (hosted story memory keeps it and reads on). */
const UNREMEMBERED ="This chapter slipped past me, so I couldn’t make notes on it. You can still ask me about it in the chat.";

/** Whether the rest of this book's memory waits for tomorrow (the server answers
 * `status: "waiting"` once a reader's allowance for the day is spent). Asked only when what came
 * back is behind the reader; a server that never waits never shows the line. */
function useWaiting(bookId: string, behind: boolean) {
  const [waiting, setWaiting] = useState(false);
  useEffect(() => {
    setWaiting(false);
    if (!behind) return;
    let live = true;
    Promise.resolve()
      .then(() => api.ingest(bookId))
      .then((state) => live && setWaiting(state.status === "waiting"))
      .catch(() => {});
    return () => {
      live = false;
    };
  }, [bookId, behind]);
  return waiting;
}

export function PeoplePanel({
  bookId,
  bookmark,
  refresh = 0,
  focusId,
  onAsk,
}: {
  bookId: string;
  bookmark: number;
  /** Changes when more of the book has been extracted: fetch again, keeping what is shown. */
  refresh?: number;
  focusId?: EntityId | null;
  onAsk: (name: string) => void;
}) {
  const [people, setPeople] = useState<GraphNode[] | null>(null);
  const [asOf, setAsOf] = useState<number | null>(null);
  const [failed, setFailed] = useState<Failure>(false);
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<EntityId | null>(focusId ?? null);
  const [detail, setDetail] = useState<Character | null>(null);
  const [detailFailed, setDetailFailed] = useState(false);
  const waiting = useWaiting(bookId, asOf != null && asOf < bookmark);

  useEffect(() => setSelected(focusId ?? null), [focusId]);
  const shownFor = useRef("");
  useEffect(() => {
    let live = true;
    if (shownFor.current !== `${bookId}:${bookmark}`) setPeople(null);
    shownFor.current = `${bookId}:${bookmark}`;
    setFailed(false);
    api
      .graph(bookId, bookmark)
      .then((graph) => {
        if (live && graph.as_of_chapter <= bookmark) {
          setPeople(graph.characters.filter((node) => node.type === "character" || !node.type));
          setAsOf(graph.as_of_chapter);
        }
      })
      .catch((reason: unknown) => live && setFailed(failure(reason)));
    return () => {
      live = false;
    };
  }, [bookId, bookmark, refresh]);
  useEffect(() => {
    if (selected == null) return;
    let live = true;
    setDetail(null);
    setDetailFailed(false);
    api
      .character(bookId, selected, bookmark)
      .then((value) => live && value.as_of_chapter <= bookmark && setDetail(value))
      .catch(() => live && setDetailFailed(true));
    return () => {
      live = false;
    };
  }, [bookId, bookmark, selected]);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const list = [...(people ?? [])].sort((a, b) => a.revealed_at - b.revealed_at || byId(a.entity_id, b.entity_id));
    if (!needle) return list;
    return list.filter((person) =>
      [person.canonical_name, ...(person.aliases ?? [])].some((name) => name.toLowerCase().includes(needle)),
    );
  }, [people, query]);

  if (selected != null) {
    const base = people?.find((person) => person.entity_id === selected);
    const name = detail?.name ?? base?.canonical_name ?? "";
    const groups = new Map<string, Character["ties"]>();
    for (const tie of detail?.ties ?? []) {
      const label = groupLabel(tie.rel_type);
      groups.set(label, [...(groups.get(label) ?? []), tie]);
    }
    return (
      <div className="memory-pane memory-person">
        <button type="button" className="memory-back" onClick={() => setSelected(null)}>
          <Icon name="arrowLeft" size={15} />
          Everyone
        </button>
        {detailFailed ? (
          <MemoryState mood="sleepy">I couldn’t open this person’s page. Try again in a moment.</MemoryState>
        ) : !detail ? (
          <MemoryState mood="thinking">Remembering…</MemoryState>
        ) : (
          <>
            <header className="memory-person-head">
              <PersonAvatar id={detail.entity_id} name={name} size={64} />
              <div>
                <h3 className="lp-voice">{name}</h3>
                {detail.aliases.length > 0 && <p className="memory-aliases">Also called {detail.aliases.slice(0, 4).join(", ")}</p>}
                <p className="memory-met">You met them in chapter {detail.first_seen}</p>
              </div>
            </header>
            <button type="button" className="lp-lamp-button memory-ask" onClick={() => onAsk(name)}>
              <Moth mood="curious" size={22} glow={false} />
              Ask Mothli about {shortName(name)}
            </button>
            {groups.size === 0 ? (
              <p className="memory-empty-line">No connections yet. They may matter more later.</p>
            ) : (
              [...groups.entries()].map(([label, ties]) => (
                <section className="memory-ties" key={label} aria-label={label}>
                  <h4>{label}</h4>
                  <ul>
                    {ties.map((tie) => (
                      <li key={`${tie.entity_id}-${tie.label}`}>
                        <button type="button" onClick={() => setSelected(tie.entity_id)}>
                          <PersonAvatar id={tie.entity_id} name={tie.name} size={30} />
                          <span>
                            <strong>{tie.name}</strong>
                            <small>{tie.label}</small>
                          </span>
                        </button>
                      </li>
                    ))}
                  </ul>
                </section>
              ))
            )}
          </>
        )}
      </div>
    );
  }

  return (
    <div className="memory-pane">
      <p className="memory-intro">
        Everyone you’ve met, in the order you met them. New faces join as you read.
      </p>
      <label className="memory-search">
        <Icon name="search" size={15} />
        <span className="sr-only">Find a person</span>
        <input value={query} onChange={(event) => setQuery(event.currentTarget.value)} placeholder="Find someone" />
      </label>
      {failed === "absent" ? (
        <MemoryState mood="curious">I don’t keep a list of who’s who for this book yet. Ask me about anyone you’ve met in the chat.</MemoryState>
      ) : failed ? (
        <MemoryState mood="sleepy">I couldn’t gather the cast right now. Try again in a moment.</MemoryState>
      ) : people == null ? (
        <MemoryState mood="thinking">Gathering everyone you’ve met…</MemoryState>
      ) : people.length === 0 ? (
        <MemoryState mood="curious">{waiting ? WAITING : "No one yet. Keep reading and I’ll keep track of who’s who."}</MemoryState>
      ) : (
        <>
          {waiting && <p className="memory-intro" role="status">{WAITING}</p>}
          <p className="memory-collection-count">
            <strong>{people.length}</strong> {people.length === 1 ? "person" : "people"} in your cast
          </p>
          <ul className="memory-people memory-collection" aria-label="People you’ve met">
            {filtered.map((person) => (
              <li key={person.entity_id}>
                <button type="button" onClick={() => setSelected(person.entity_id)}>
                  {person.revealed_at >= bookmark && <span className="memory-new">New</span>}
                  <PersonAvatar id={person.entity_id} name={person.canonical_name} size={52} />
                  <strong>{person.canonical_name}</strong>
                  <small>Chapter {person.revealed_at}</small>
                </button>
              </li>
            ))}
            {filtered.length === 0 && <li className="memory-empty-line">Nobody by that name yet.</li>}
          </ul>
        </>
      )}
    </div>
  );
}

export function StoryPanel({
  bookId,
  bookmark,
  refresh = 0,
  onOpenPerson,
  onAskChapter,
}: {
  bookId: string;
  bookmark: number;
  /** Changes when more of the book has been extracted: fetch again, keeping what is shown. */
  refresh?: number;
  onOpenPerson: (id: EntityId) => void;
  onAskChapter: (title: string) => void;
}) {
  const [chapters, setChapters] = useState<ChapterNote[] | null>(null);
  const [asOf, setAsOf] = useState<number | null>(null);
  const [failed, setFailed] = useState<Failure>(false);
  const [open, setOpen] = useState<string | null>(null);
  const waiting = useWaiting(bookId, asOf != null && asOf < bookmark);

  const shownFor = useRef("");
  useEffect(() => {
    let live = true;
    if (shownFor.current !== `${bookId}:${bookmark}`) setChapters(null);
    shownFor.current = `${bookId}:${bookmark}`;
    setFailed(false);
    api
      .notes(bookId, bookmark)
      .then((notes) => {
        if (live && notes.as_of_chapter <= bookmark) {
          setChapters([...notes.chapters].filter((chapter) => chapter.revealed_at <= bookmark).reverse());
          setAsOf(notes.as_of_chapter);
        }
      })
      .catch((reason: unknown) => live && setFailed(failure(reason)));
    return () => {
      live = false;
    };
  }, [bookId, bookmark, refresh]);

  if (failed === "absent")
    return <MemoryState mood="curious">I don’t keep chapter-by-chapter notes for this book yet. Ask me about any chapter you’ve read in the chat.</MemoryState>;
  if (failed) return <MemoryState mood="sleepy">I couldn’t open my notes right now. Try again in a moment.</MemoryState>;
  if (!chapters) return <MemoryState mood="thinking">Flipping back through my notes…</MemoryState>;
  if (!chapters.length)
    return <MemoryState mood="curious">{waiting ? WAITING : "Finish your first chapter and I’ll start keeping notes for you here."}</MemoryState>;

  return (
    <div className="memory-pane">
      <p className="memory-intro">What happened, chapter by chapter. Your latest chapter is first.</p>
      {waiting && <p className="memory-intro" role="status">{WAITING}</p>}
      <ol className="memory-timeline" aria-label="The story so far">
        {chapters.map((chapter, index) => {
          const expanded = open === chapter.chapter_key;
          return (
            <li key={chapter.chapter_key} className={index === 0 ? "is-latest" : undefined}>
              <span className="memory-timeline-dot" aria-hidden="true">{chapter.revealed_at}</span>
              <article>
                <h3>{chapter.title || `Chapter ${chapter.revealed_at}`}</h3>
                {chapter.unremembered ? (
                  <p className="lp-voice">{UNREMEMBERED}</p>
                ) : (
                  <p className={expanded ? undefined : "is-clamped"}>{chapter.summary}</p>
                )}
                {expanded && chapter.events.length > 0 && (
                  <ul className="memory-events">
                    {chapter.events.map((event) => (
                      <li key={event}>{event}</li>
                    ))}
                  </ul>
                )}
                {chapter.new_characters.length > 0 && (
                  <div className="memory-chips" aria-label="First appearances">
                    {chapter.new_characters.slice(0, expanded ? undefined : 4).map((person) => (
                      <button type="button" key={person.entity_id} onClick={() => onOpenPerson(person.entity_id)}>
                        <PersonAvatar id={person.entity_id} name={person.name} size={20} />
                        {person.name}
                      </button>
                    ))}
                  </div>
                )}
                <div className="memory-chapter-actions">
                  <button type="button" onClick={() => setOpen(expanded ? null : chapter.chapter_key)} aria-expanded={expanded}>
                    {expanded ? "Show less" : "Show more"}
                  </button>
                  <button type="button" className="memory-ask-inline" onClick={() => onAskChapter(chapter.title || `chapter ${chapter.revealed_at}`)}>
                    <Moth mood="curious" size={18} glow={false} />
                    Talk about this
                  </button>
                </div>
              </article>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
