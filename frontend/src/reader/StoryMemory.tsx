/** Mothli's memory, made visible: the people you've met and the story so far. Everything here is
 * served by bookmark-clamped routes (graph, character, notes, catch-me-up), so nothing past the
 * reader's frontier can appear. Read-only; asking about something hands off to the conversation. */
import { useEffect, useMemo, useState } from "react";
import { api, type ChapterNote, type Character, type GraphNode } from "../api";
import { Icon } from "../ui/Icon";
import { Moth } from "../ui/Moth";

const TONES = ["wing", "moss", "dusk", "rose", "sky"] as const;
function toneFor(id: number) {
  return TONES[Math.abs(id * 7 + 3) % TONES.length];
}
function initials(name: string) {
  const parts = name.replace(/[^\p{L}\s'-]/gu, "").trim().split(/\s+/);
  return ((parts[0]?.[0] ?? "?") + (parts.length > 1 ? parts[parts.length - 1][0] : "")).toUpperCase();
}
export function shortName(name: string) {
  return name.split(/\s+/)[0] ?? name;
}

export function PersonAvatar({ id, name, size = 40 }: { id: number; name: string; size?: number }) {
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

function MemoryState({ mood, children }: { mood: "thinking" | "sleepy" | "curious"; children: React.ReactNode }) {
  return (
    <div className="memory-state">
      <Moth mood={mood} size={64} />
      <p className="lp-voice">{children}</p>
    </div>
  );
}

export function PeoplePanel({
  bookId,
  bookmark,
  focusId,
  onAsk,
}: {
  bookId: string;
  bookmark: number;
  focusId?: number | null;
  onAsk: (name: string) => void;
}) {
  const [people, setPeople] = useState<GraphNode[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<number | null>(focusId ?? null);
  const [detail, setDetail] = useState<Character | null>(null);
  const [detailFailed, setDetailFailed] = useState(false);

  useEffect(() => setSelected(focusId ?? null), [focusId]);
  useEffect(() => {
    let live = true;
    setPeople(null);
    setFailed(false);
    api
      .graph(bookId, bookmark)
      .then((graph) => {
        if (live && graph.as_of_chapter <= bookmark)
          setPeople(graph.characters.filter((node) => node.type === "character" || !node.type));
      })
      .catch(() => live && setFailed(true));
    return () => {
      live = false;
    };
  }, [bookId, bookmark]);
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
    const list = [...(people ?? [])].sort((a, b) => a.revealed_at - b.revealed_at || a.entity_id - b.entity_id);
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
      {failed ? (
        <MemoryState mood="sleepy">I couldn’t gather the cast right now. Try again in a moment.</MemoryState>
      ) : people == null ? (
        <MemoryState mood="thinking">Gathering everyone you’ve met…</MemoryState>
      ) : people.length === 0 ? (
        <MemoryState mood="curious">No one yet. Keep reading and I’ll keep track of who’s who.</MemoryState>
      ) : (
        <>
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
  onOpenPerson,
  onAskChapter,
}: {
  bookId: string;
  bookmark: number;
  onOpenPerson: (id: number) => void;
  onAskChapter: (title: string) => void;
}) {
  const [chapters, setChapters] = useState<ChapterNote[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [open, setOpen] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    setChapters(null);
    setFailed(false);
    api
      .notes(bookId, bookmark)
      .then((notes) => {
        if (live && notes.as_of_chapter <= bookmark)
          setChapters([...notes.chapters].filter((chapter) => chapter.revealed_at <= bookmark).reverse());
      })
      .catch(() => live && setFailed(true));
    return () => {
      live = false;
    };
  }, [bookId, bookmark]);

  if (failed) return <MemoryState mood="sleepy">I couldn’t open my notes right now. Try again in a moment.</MemoryState>;
  if (!chapters) return <MemoryState mood="thinking">Flipping back through my notes…</MemoryState>;
  if (!chapters.length)
    return <MemoryState mood="curious">Finish your first chapter and I’ll start keeping notes for you here.</MemoryState>;

  return (
    <div className="memory-pane">
      <p className="memory-intro">What happened, chapter by chapter. Your latest chapter is first.</p>
      <ol className="memory-timeline" aria-label="The story so far">
        {chapters.map((chapter, index) => {
          const expanded = open === chapter.chapter_key;
          return (
            <li key={chapter.chapter_key} className={index === 0 ? "is-latest" : undefined}>
              <span className="memory-timeline-dot" aria-hidden="true">{chapter.revealed_at}</span>
              <article>
                <h3>{chapter.title || `Chapter ${chapter.revealed_at}`}</h3>
                <p className={expanded ? undefined : "is-clamped"}>{chapter.summary}</p>
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
