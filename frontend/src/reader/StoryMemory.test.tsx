import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, test, vi } from "vitest";

const mocks = vi.hoisted(() => ({ graph: vi.fn(), character: vi.fn(), notes: vi.fn(), ingest: vi.fn() }));
vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: mocks,
}));
import { ApiError, type EntityId } from "../api";
import { CATCHING_UP, PeoplePanel, StoryPanel, titleWords, toneFor } from "./StoryMemory";

// Hosted ids are UUIDs (ADR 0042 part 4); local ids are numbers.
const MARA = "6f1c2a7e-0b7d-4c55-9e3b-1d2f3a4b5c6d";
const CORVUS = "0a9b8c7d-6e5f-4a3b-8c2d-1e0f9a8b7c6d";

const person = (entity_id: EntityId, canonical_name: string, revealed_at: number) =>
  ({ entity_id, canonical_name, type: "character", revealed_at, aliases: [] });
const graph = (as_of_chapter: number, characters: ReturnType<typeof person>[]) =>
  ({ as_of_chapter, characters, relationships: [] });
const card = (entity_id: EntityId, name: string, ties: { entity_id: EntityId; name: string }[] = []) => ({
  as_of_chapter: 3, entity_id, name, type: "character", aliases: [], first_seen: 3, status: null,
  ties: ties.map((tie) => ({ ...tie, rel_type: "friend", label: "walks with", direction: "out" as const })),
});
const progress = (status: string) => ({ ingest_progress: 3, status, flags: [], error: null });
const missing = () => new ApiError("unknown book", 404);

beforeEach(() => {
  for (const mock of Object.values(mocks)) mock.mockReset();
  mocks.ingest.mockResolvedValue(progress("idle"));
});

describe("the cast and the story with hosted ids", () => {
  test("a UUID goes through the cast, a person's card and a tie to the next card", async () => {
    mocks.graph.mockResolvedValue(graph(3, [person(MARA, "Mara", 3), person(CORVUS, "Corvus", 3)]));
    mocks.character.mockImplementation(async (_book: string, id: EntityId) =>
      id === MARA ? card(MARA, "Mara", [{ entity_id: CORVUS, name: "Corvus" }]) : card(CORVUS, "Corvus"));
    const { container } = render(<PeoplePanel bookId="b" bookmark={3} onAsk={() => {}} />);
    // Same chapter: the server's own order stands (a UUID says nothing about who came first).
    const names = (await screen.findAllByRole("button", { name: /Chapter 3/ })).map((item) => item.textContent);
    expect(names).toEqual(["NewMMaraChapter 3", "NewCCorvusChapter 3"]);
    const tones = [...container.querySelectorAll(".memory-avatar")].map((item) => item.className);
    expect(tones).toEqual([`memory-avatar memory-avatar--${toneFor(MARA)}`,
                           `memory-avatar memory-avatar--${toneFor(CORVUS)}`]);
    expect(tones.join(" ")).not.toMatch(/undefined|NaN/);
    fireEvent.click(screen.getByRole("button", { name: /Mara/ }));
    await screen.findByRole("heading", { name: "Mara" });
    expect(mocks.character).toHaveBeenLastCalledWith("b", MARA, 3);
    fireEvent.click(await screen.findByRole("button", { name: /Corvus/ }));
    await screen.findByRole("heading", { name: "Corvus" });
    expect(mocks.character).toHaveBeenLastCalledWith("b", CORVUS, 3);
  });

  test("a UUID in the story so far opens that person", async () => {
    mocks.notes.mockResolvedValue({
      as_of_chapter: 3, cast: [{ name: "Mara", entity_id: MARA }],
      chapters: [{ chapter_key: "k3", revealed_at: 3, title: "Down the Lamp-Hole", summary: "Mara falls.",
                   new_characters: [{ entity_id: MARA, name: "Mara" }], events: [] }],
    });
    const opened = vi.fn();
    render(<StoryPanel bookId="b" bookmark={3} onOpenPerson={opened} onAskChapter={() => {}} />);
    fireEvent.click(await screen.findByRole("button", { name: /Mara/ }));
    expect(opened).toHaveBeenCalledWith(MARA);
  });

  test("a hosted id always gets the same colour, and a number keeps the colour it had", () => {
    expect(toneFor(MARA)).toBe(toneFor(`${MARA}`));
    // The local formula, unchanged: tones[|7n + 3| mod 5].
    expect([1, 2, 3, 4, 5, -1].map(toneFor)).toEqual(["wing", "dusk", "sky", "moss", "rose", "sky"]);
  });
});

describe("a chapter Mothli could not make notes on", () => {
  const UNREMEMBERED = "This chapter slipped past me, so I couldn’t make notes on it. You can still ask me about it in the chat.";

  test("the story says so kindly in place of its empty summary, and only for that chapter", async () => {
    mocks.notes.mockResolvedValue({ as_of_chapter: 3, cast: [], chapters: [
      { chapter_key: "k2", revealed_at: 2, title: "Two", summary: "Mara sails.", new_characters: [], events: [] },
      { chapter_key: "k3", revealed_at: 3, title: "Three", summary: "", new_characters: [], events: [],
        unremembered: true }] });
    render(<StoryPanel bookId="b" bookmark={3} onOpenPerson={() => {}} onAskChapter={() => {}} />);
    await screen.findByText(UNREMEMBERED);
    expect(screen.getAllByText(UNREMEMBERED)).toHaveLength(1);
    expect(screen.getByText("Mara sails.")).toBeTruthy();
  });

  test("a chapter that is not marked keeps its summary, as local mode always does", async () => {
    mocks.notes.mockResolvedValue({ as_of_chapter: 1, cast: [], chapters: [
      { chapter_key: "k1", revealed_at: 1, title: "One", summary: "", new_characters: [], events: [] }] });
    render(<StoryPanel bookId="b" bookmark={1} onOpenPerson={() => {}} onAskChapter={() => {}} />);
    await screen.findByRole("heading", { name: "One" });
    expect(screen.queryByText(UNREMEMBERED)).toBeNull();
  });
});

// ADR 0044 step 5: every book has a cast and a story, built as readers reach its chapters, so
// before the first finished chapter the server answers an empty one for every book alike,
// whether or not anyone else has the same file.
const NO_ONE_YET = "No one yet. Keep reading and I’ll keep track of who’s who.";
const FIRST_CHAPTER = "Finish your first chapter and I’ll start keeping notes for you here.";

describe("a book before its first finished chapter", () => {
  test("the cast and the story show the same empty lines every book shows", async () => {
    mocks.graph.mockResolvedValue(graph(0, []));
    mocks.notes.mockResolvedValue({ as_of_chapter: 0, cast: [], chapters: [] });
    render(<PeoplePanel bookId="b" bookmark={0} place={0} onAsk={() => {}} />);
    await screen.findByText(NO_ONE_YET);
    render(<StoryPanel bookId="b" bookmark={0} place={0} onOpenPerson={() => {}} onAskChapter={() => {}} />);
    await screen.findByText(FIRST_CHAPTER);
    expect(screen.queryByText(/Try again/)).toBeNull();
    expect(screen.queryByText(CATCHING_UP)).toBeNull();
    // Nothing is behind the reader, so nobody asks whether more is on its way.
    expect(mocks.ingest).not.toHaveBeenCalled();
  });

  test("no line says this book has no notes, not even on a refusal", async () => {
    mocks.graph.mockRejectedValue(missing());
    mocks.notes.mockRejectedValue(missing());
    render(<PeoplePanel bookId="b" bookmark={3} place={3} onAsk={() => {}} />);
    await screen.findByText("I couldn’t gather the cast right now. Try again in a moment.");
    render(<StoryPanel bookId="b" bookmark={3} place={3} onOpenPerson={() => {}} onAskChapter={() => {}} />);
    await screen.findByText("I couldn’t open my notes right now. Try again in a moment.");
    expect(screen.queryByText(/for this book yet/)).toBeNull();
    expect(screen.queryByText(CATCHING_UP)).toBeNull();
  });
});

// 2026-11-20: a reader who says they have read 30 chapters waits a while for the notes to
// catch up. Whatever the reason the notes are behind, the screens show what there is and one
// line, the same every time, worked out only from what is shown: how far the notes reach and how
// far the reader is. Nothing is asked of the server for it, so it can say nothing more.
describe("notes still catching up with the reader", () => {
  test("the cast and the story show what there is, and say once that Mothli is catching up", async () => {
    mocks.graph.mockResolvedValue(graph(3, [person(MARA, "Mara", 3)]));
    mocks.notes.mockResolvedValue({ as_of_chapter: 3, cast: [], chapters: [
      { chapter_key: "k3", revealed_at: 3, title: "Three", summary: "Mara falls.", new_characters: [], events: [] }] });
    render(<PeoplePanel bookId="b" bookmark={5} place={5} onAsk={() => {}} />);
    await screen.findByText(CATCHING_UP);
    expect(screen.getByText("Mara")).toBeTruthy();
    render(<StoryPanel bookId="b" bookmark={5} place={5} onOpenPerson={() => {}} onAskChapter={() => {}} />);
    await waitFor(() => expect(screen.getAllByText(CATCHING_UP)).toHaveLength(2));
    expect(screen.getByText("Mara falls.")).toBeTruthy();
    expect(mocks.ingest).not.toHaveBeenCalled();
  });

  test("with nothing built yet, the same line takes the place of the empty lines", async () => {
    mocks.graph.mockResolvedValue(graph(0, []));
    mocks.notes.mockResolvedValue({ as_of_chapter: 0, cast: [], chapters: [] });
    render(<PeoplePanel bookId="b" bookmark={30} place={30} onAsk={() => {}} />);
    await screen.findByText(CATCHING_UP);
    expect(screen.queryByText(/No one yet/)).toBeNull();
    render(<StoryPanel bookId="b" bookmark={30} place={30} onOpenPerson={() => {}} onAskChapter={() => {}} />);
    await waitFor(() => expect(screen.getAllByText(CATCHING_UP)).toHaveLength(2));
    expect(screen.queryByText(/Finish your first chapter/)).toBeNull();
  });

  test("the line is the same whatever the server says about building", async () => {
    for (const status of ["running", "waiting", "idle", "error"]) {
      mocks.ingest.mockResolvedValue(progress(status));
      mocks.graph.mockResolvedValue(graph(2, [person(MARA, "Mara", 1)]));
      const { container, unmount } = render(<PeoplePanel bookId="b" bookmark={4} place={4} onAsk={() => {}} />);
      await screen.findByText(CATCHING_UP);
      expect(container.querySelector("[role=status]")?.textContent).toBe(CATCHING_UP);
      unmount();
    }
  });

  test("no line about limits, costs, tomorrow or anyone else, ever", () => {
    expect(CATCHING_UP).not.toMatch(/tomorrow|limit|pay|cost|plan|quota|other|someone|anyone|reader/i);
  });

  test("notes that reach the reader's place say nothing more, even mid-chapter", async () => {
    // The reader is partway into chapter 5 (the views are asked as of 5) but has finished 4.
    mocks.graph.mockResolvedValue(graph(4, [person(MARA, "Mara", 2)]));
    render(<PeoplePanel bookId="b" bookmark={5} place={4} onAsk={() => {}} />);
    await screen.findByText("Mara");
    expect(screen.queryByText(CATCHING_UP)).toBeNull();
  });
});

describe("local mode is as it was", () => {
  test("numbers sort by chapter then id, and the panel alone never shows the catching-up line", async () => {
    for (const status of ["idle", "running", "error"]) {
      mocks.ingest.mockResolvedValue(progress(status));
      mocks.graph.mockResolvedValue(graph(2, [person(7, "Zed", 2), person(3, "Ann", 2), person(9, "Bo", 1)]));
      const { unmount } = render(<PeoplePanel bookId="b" bookmark={4} onAsk={() => {}} />);
      const names = (await screen.findAllByRole("button", { name: /Chapter/ })).map((item) => item.textContent);
      expect(names).toEqual(["BBoChapter 1", "AAnnChapter 2", "ZZedChapter 2"]);
      expect(screen.queryByText(CATCHING_UP)).toBeNull();
      unmount();
    }
  });

  test("a memory that has caught up with the reader asks nothing more", async () => {
    mocks.graph.mockResolvedValue(graph(4, [person(1, "Ann", 1)]));
    mocks.character.mockResolvedValue(card(1, "Ann"));
    render(<PeoplePanel bookId="b" bookmark={4} onAsk={() => {}} />);
    fireEvent.click(await screen.findByRole("button", { name: /Ann/ }));
    await screen.findByRole("heading", { name: "Ann" });
    expect(mocks.character).toHaveBeenCalledWith("b", 1, 4);
    expect(mocks.ingest).not.toHaveBeenCalled();
  });
});

// 2026-11-27: a book with front matter showed "4 CHAPTER II. The Pool of Tears", the piece's place
// in the file beside the title's own number. Now one number, the book's own, as the footer counts.
describe("chapter numbers in the story so far and the cast", () => {
  const ownNumber = (ordinal: number) => (ordinal > 2 ? `Chapter ${ordinal - 2}` : null);

  test("a card shows the book's own chapter number once, and the title without its own", async () => {
    mocks.notes.mockResolvedValue({ as_of_chapter: 5, cast: [], chapters: [
      { chapter_key: "k3", revealed_at: 3, title: "CHAPTER I. Down the Rabbit-Hole", summary: "A fall.", new_characters: [], events: [] },
      { chapter_key: "k4", revealed_at: 4, title: "CHAPTER II. The Pool of Tears", summary: "Tears.", new_characters: [], events: [] },
      { chapter_key: "k5", revealed_at: 5, title: "", summary: "A race.", new_characters: [], events: [] }] });
    const asked = vi.fn();
    const { container } = render(<StoryPanel bookId="b" bookmark={5} chapterName={ownNumber} onOpenPerson={() => {}} onAskChapter={asked} />);
    await screen.findByText("The Pool of Tears");
    expect([...container.querySelectorAll(".memory-chapter-head")].map((head) => head.textContent)).toEqual([
      "Chapter 3", "Chapter 2The Pool of Tears", "Chapter 1Down the Rabbit-Hole",
    ]);
    expect(container.querySelector(".memory-timeline-dot")?.textContent).toBe("");
    expect(screen.queryByText(/^[45]$/)).toBeNull();
    fireEvent.click(screen.getAllByRole("button", { name: /Talk about this/ })[0]);
    expect(asked).toHaveBeenCalledWith("Chapter 3");
  });

  test("the cast names the chapter someone was met in the same way", async () => {
    mocks.graph.mockResolvedValue(graph(5, [person(MARA, "Mara", 4)]));
    render(<PeoplePanel bookId="b" bookmark={5} chapterName={ownNumber} onAsk={() => {}} />);
    expect(await screen.findByRole("button", { name: /Mara.*Chapter 2/ })).toBeTruthy();
  });

  test("only a title's leading chapter label is taken off", () => {
    expect(titleWords("CHAPTER IV. The Rabbit Sends in a Little Bill")).toBe("The Rabbit Sends in a Little Bill");
    expect(titleWords("Chapter 12: Home")).toBe("Home");
    expect(titleWords("Chapter Ivy Grows")).toBe("Chapter Ivy Grows");
    expect(titleWords("CHAPTER XII")).toBe("CHAPTER XII");
    expect(titleWords("The Chapter House")).toBe("The Chapter House");
  });
});
