import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, test, vi } from "vitest";

const mocks = vi.hoisted(() => ({ graph: vi.fn(), character: vi.fn(), notes: vi.fn(), ingest: vi.fn() }));
vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: mocks,
}));
import { ApiError, type EntityId } from "../api";
import { PeoplePanel, StoryPanel, toneFor } from "./StoryMemory";

// Hosted ids are UUIDs (ADR 0042 part 4); local ids are numbers.
const MARA = "6f1c2a7e-0b7d-4c55-9e3b-1d2f3a4b5c6d";
const CORVUS = "0a9b8c7d-6e5f-4a3b-8c2d-1e0f9a8b7c6d";
const WAITING = "I’m a little behind you. I’ll catch up on the rest tomorrow.";

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
    render(<PeoplePanel bookId="b" bookmark={0} onAsk={() => {}} />);
    await screen.findByText(NO_ONE_YET);
    render(<StoryPanel bookId="b" bookmark={0} onOpenPerson={() => {}} onAskChapter={() => {}} />);
    await screen.findByText(FIRST_CHAPTER);
    expect(screen.queryByText(/Try again/)).toBeNull();
    expect(screen.queryByText(WAITING)).toBeNull();
    // Nothing is behind the reader, so nobody asks whether more is on its way.
    expect(mocks.ingest).not.toHaveBeenCalled();
  });

  test("no line says this book has no notes, not even on a refusal", async () => {
    mocks.graph.mockRejectedValue(missing());
    mocks.notes.mockRejectedValue(missing());
    render(<PeoplePanel bookId="b" bookmark={3} onAsk={() => {}} />);
    await screen.findByText("I couldn’t gather the cast right now. Try again in a moment.");
    render(<StoryPanel bookId="b" bookmark={3} onOpenPerson={() => {}} onAskChapter={() => {}} />);
    await screen.findByText("I couldn’t open my notes right now. Try again in a moment.");
    expect(screen.queryByText(/for this book yet/)).toBeNull();
    expect(screen.queryByText(WAITING)).toBeNull();
  });
});

describe("a memory that waits for tomorrow", () => {
  test("the cast and the story say once that Mothli will catch up tomorrow", async () => {
    mocks.ingest.mockResolvedValue(progress("waiting"));
    mocks.graph.mockResolvedValue(graph(3, [person(MARA, "Mara", 3)]));
    mocks.notes.mockResolvedValue({ as_of_chapter: 3, cast: [], chapters: [
      { chapter_key: "k3", revealed_at: 3, title: "Three", summary: "Mara falls.", new_characters: [], events: [] }] });
    render(<PeoplePanel bookId="b" bookmark={5} onAsk={() => {}} />);
    await screen.findByText(WAITING);
    expect(screen.getByText("Mara")).toBeTruthy();
    render(<StoryPanel bookId="b" bookmark={5} onOpenPerson={() => {}} onAskChapter={() => {}} />);
    await waitFor(() => expect(screen.getAllByText(WAITING)).toHaveLength(2));
    expect(mocks.ingest).toHaveBeenCalledWith("b");
  });

  test("with nothing built yet, the line takes the place of “no one yet”", async () => {
    mocks.ingest.mockResolvedValue(progress("waiting"));
    mocks.graph.mockResolvedValue(graph(0, []));
    render(<PeoplePanel bookId="b" bookmark={2} onAsk={() => {}} />);
    await screen.findByText(WAITING);
    expect(screen.queryByText(/No one yet/)).toBeNull();
  });
});

describe("local mode is as it was", () => {
  test("numbers sort by chapter then id, and no local status ever shows the waiting line", async () => {
    for (const status of ["idle", "running", "error"]) {
      mocks.ingest.mockResolvedValue(progress(status));
      mocks.graph.mockResolvedValue(graph(2, [person(7, "Zed", 2), person(3, "Ann", 2), person(9, "Bo", 1)]));
      const { unmount } = render(<PeoplePanel bookId="b" bookmark={4} onAsk={() => {}} />);
      const names = (await screen.findAllByRole("button", { name: /Chapter/ })).map((item) => item.textContent);
      expect(names).toEqual(["BBoChapter 1", "AAnnChapter 2", "ZZedChapter 2"]);
      await waitFor(() => expect(mocks.ingest).toHaveBeenCalled());
      expect(screen.queryByText(WAITING)).toBeNull();
      unmount();
      mocks.ingest.mockClear();
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
