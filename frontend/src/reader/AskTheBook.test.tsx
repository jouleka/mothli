import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, test, vi } from "vitest";
import { axeAA } from "../test-a11y";
const mocks = vi.hoisted(() => ({
  askBook: vi.fn(),
  askBookStream: vi.fn(),
  bookConversation: vi.fn(),
  clearConversation: vi.fn(),
  selectionAction: vi.fn(),
  catchMeUp: vi.fn(),
  graph: vi.fn(),
  visiblePeople: vi.fn(),
  chapterCloseout: vi.fn(),
  notes: vi.fn(),
  owner: vi.fn(),
}));
vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: mocks,
}));
import { ApiError } from "../api";
vi.mock("../pwa/offline", () => ({ activeOfflineOwner: mocks.owner }));
import { AskTheBook, type AskSession } from "./AskTheBook";
const answer = {
  as_of_chapter: 2,
  insufficient_evidence: false,
  claims: [{ text: "Berenice met Aldric at the forge.", citation_ids: [1] }],
  citations: [
    {
      id: 1,
      ordinal: 2,
      chapter_key: "two",
      href: "c2.xhtml",
      title: "The forge",
      excerpt: "They met at the forge and spoke quietly.",
    },
  ],
  cost: null,
};
const props = {
  bookId: "b",
  bookmark: 2,
  embedded: true,
  onNavigate: vi.fn(),
  onClose: vi.fn(),
};
const point = { id: "a".repeat(64), atom: 2, offset: 120, position_epoch: 0, available_chapter: 2 };
/** What `GET /api/books/b/conversation` answers: each pair is a question and the reply kept for it. */
function stored(...pairs: [string, string | null][]) {
  return {
    read_frontier: point,
    turns: pairs.flatMap(([question, reply], index) => [
      { role: "user" as const, content: question, atom: 2, reaction: false, created_at: `2020-01-01T10:0${index}:00+00:00` },
      ...(reply === null ? [] : [
        { role: "assistant" as const, content: reply, atom: 2, reaction: false, created_at: `2020-01-01T10:0${index}:01+00:00` },
      ]),
    ]),
  };
}
function submit(value: string) {
  fireEvent.change(screen.getByLabelText("Message Mothli"), { target: { value } });
  fireEvent.submit(screen.getByLabelText("Message Mothli").closest("form")!);
}
beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  mocks.owner.mockReturnValue("local");
  mocks.askBook.mockResolvedValue(answer);
  mocks.bookConversation.mockReset().mockResolvedValue(stored());
  mocks.askBookStream.mockReset();
  mocks.clearConversation.mockReset().mockResolvedValue(undefined);
  mocks.catchMeUp.mockResolvedValue({
    as_of_chapter: 2,
    cast: [{ name: "Aldric", entity_id: 1 }],
  });
  mocks.graph.mockResolvedValue({as_of_chapter:2,characters:[{canonical_name:"Aldric",entity_id:1}],relationships:[]});
});

describe("Mothli conversation", () => {
  test("shows a computed answer alongside the separately cited book answer", async () => {
    mocks.askBook.mockResolvedValueOnce({...answer, calculation:"2 + 2 = 4."});
    render(<AskTheBook {...props} />);
    submit("What is 2 + 2, and who met at the forge?");
    await screen.findByText(/2 \+ 2 = 4\./);
    expect(screen.getByText(/Berenice met Aldric at the forge/)).toBeTruthy();
    expect(screen.getByText("Pages")).toBeTruthy();
  });
  test("finds names from confirmed pages when cold memory has no graph", async () => {
    const point={id:"a".repeat(64),atom:1,offset:100,position_epoch:0,available_chapter:1};
    mocks.graph.mockResolvedValueOnce({as_of_chapter:0,characters:[],relationships:[]});
    mocks.visiblePeople.mockResolvedValue({read_frontier:point,names:["Mara"]});
    render(<AskTheBook {...props} bookmark={1} prepareReadContext={vi.fn(async()=>point)} />);
    fireEvent.click(screen.getByRole("button",{name:"Who was that?"}));
    await screen.findByRole("button",{name:"Mara"});
    expect(mocks.visiblePeople).toHaveBeenCalledWith("b",point);
    expect(mocks.catchMeUp).not.toHaveBeenCalled();
    expect(mocks.askBook).not.toHaveBeenCalled();
  });

  test("shows a fact-free passage clarification without citations or generic failure", async () => {
    const message = "Do you mean the name’s meaning, or whether the character was based on a real person?";
    mocks.selectionAction.mockResolvedValue({as_of_chapter:2,insufficient_evidence:true,
      text:null,citation:null,message,cost:null});
    render(<AskTheBook {...props} selection={{text:"What is it, Kolya? said Alyosha.",
      anchor:{atom:2,cfi:"cfi"}}} />);
    submit("What is Kolya in real life?");
    await screen.findByText(message);
    expect(screen.queryByText("I couldn’t get a reply. Try again?")).toBeNull();
    expect(screen.queryByText("Pages")).toBeNull();
  });

  test("discards a people list returned after rewinding", async () => {
    let resolve!: (value: unknown) => void;
    mocks.graph.mockImplementationOnce(() => new Promise((done) => {resolve=done;}));
    const view=render(<AskTheBook {...props} />);
    fireEvent.click(screen.getByRole("button",{name:"Who was that?"}));
    view.rerender(<AskTheBook {...props} bookmark={1} />);
    await act(async () => resolve({as_of_chapter:2,characters:[{canonical_name:"Future name",entity_id:3}]}));
    expect(screen.queryByText("Future name")).toBeNull();
    expect(mocks.catchMeUp).not.toHaveBeenCalled();
  });

  test("explains a source verification failure without publishing an unchecked answer", async () => {
    mocks.askBook.mockRejectedValue(new ApiError("reply could not be cleared against your read pages", 502));
    render(<AskTheBook {...props} />);
    submit("Who was that?");
    await screen.findByText("I couldn’t back that up from your pages, so I’d rather not guess. Try naming the person or moment you mean.");
    expect(screen.queryByText(answer.claims[0].text)).toBeNull();
  });

  test("says so when the reader asks about pages they have not reached", async () => {
    mocks.askBook.mockResolvedValue({ ...answer, insufficient_evidence: true, claims: [], citations: [], status: "ahead" });
    render(<AskTheBook {...props} />);
    submit("Who dies in the end?");
    await screen.findByText(/We haven’t got there yet\./);
  });

  test("shows a prose reply as one paragraph with its closing line", async () => {
    mocks.askBook.mockResolvedValue({ ...answer, style: "prose", closing: "Did it read that way to you?",
      claims: [{ text: "She couldn’t stand him any more.", citation_ids: [1] },
               { text: "He’d taken all her money.", citation_ids: [1] }] });
    render(<AskTheBook {...props} />);
    submit("Why did she leave?");
    await screen.findByText("She couldn’t stand him any more. He’d taken all her money. Did it read that way to you?");
  });

  test("does not replay a seeded question when the panel is reopened", async () => {
    const seed = { id: "seed-reopen-1", kind: "ask" as const, prompt: "Remind me who Aldric is" };
    const first = render(<AskTheBook {...props} seed={seed} />);
    await waitFor(() => expect(mocks.askBook).toHaveBeenCalledTimes(1));
    first.unmount();
    render(<AskTheBook {...props} seed={seed} />);
    await screen.findByLabelText("Message Mothli");
    expect(mocks.askBook).toHaveBeenCalledTimes(1);
  });

  test("shows checked sentences as they arrive and then the finished reply", async () => {
    const point = {id:"a".repeat(64), atom:2, offset:120, position_epoch:0, available_chapter:2};
    const streamed = {...answer, style:"prose" as const, citations:[], read_frontier:point,
      claims:[{text:"Darcy called her tolerable.", citation_ids:[]}, {text:"Ouch.", citation_ids:[]}]};
    let finish!: (value: typeof streamed) => void;
    let say!: (text: string) => void;
    mocks.askBookStream.mockImplementationOnce((...args: unknown[]) => {
      say = args[4] as (text: string) => void;
      return new Promise((resolve) => { finish = resolve; });
    });
    render(<AskTheBook {...props} prepareReadContext={async () => point} />);
    submit("What did he say?");
    await waitFor(() => expect(mocks.askBookStream).toHaveBeenCalled());
    act(() => say("Darcy called her tolerable."));
    await screen.findByText("Darcy called her tolerable.");
    expect(screen.getByText("Cancel")).toBeTruthy();
    await act(async () => finish(streamed));
    await screen.findByText("Darcy called her tolerable. Ouch.");
    expect(screen.queryByText("Cancel")).toBeNull();
    expect(mocks.askBook).not.toHaveBeenCalled();
    // The server keeps the conversation and ignores any the browser sends: none is sent.
    expect(mocks.askBookStream).toHaveBeenCalledWith("b", "What did he say?", 2, point, expect.any(Function), expect.any(AbortSignal));
  });

  // Catches: the hosted allowance shown as a generic failure with a Retry that cannot work.
  test("at the allowance, says the server's plain line and offers no Retry", async () => {
    const line = "Mothli has talked a lot with you today and is resting until tomorrow. Your book and your place are right here.";
    mocks.askBookStream.mockRejectedValueOnce(new ApiError(line, 429, "allowance_reached"));
    render(<AskTheBook {...props} prepareReadContext={async () => point} />);
    submit("Who was at the forge?");
    expect(await screen.findByText(line)).toBeTruthy();
    expect(screen.queryByText("Mothli needs a short break. Try again shortly.")).toBeNull();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });

  test("while the last question is still being answered, asks for a moment and keeps Retry", async () => {
    mocks.askBookStream.mockRejectedValueOnce(new ApiError("Mothli is still answering you.", 429, "companion_busy"));
    render(<AskTheBook {...props} prepareReadContext={async () => point} />);
    submit("And then?");
    expect(await screen.findByText("Give me a second — I’m still on your last question. Ask again in a moment.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
  });

  test("asks the ordinary way when the server does not offer streamed replies", async () => {
    const point = {id:"a".repeat(64), atom:2, offset:120, position_epoch:0, available_chapter:2};
    mocks.askBookStream.mockResolvedValueOnce(null);
    mocks.askBook.mockResolvedValue({...answer, read_frontier:point});
    render(<AskTheBook {...props} prepareReadContext={async () => point} />);
    submit("Where did they meet?");
    await screen.findByText(answer.claims[0].text);
    expect(mocks.askBook).toHaveBeenCalledWith("b", "Where did they meet?", 2, [], point, expect.any(AbortSignal));
  });

  test("confirms the visible page before sending and rejects a different source boundary", async () => {
    const point = {id:"a".repeat(64), atom:2, offset:120, position_epoch:0, available_chapter:2};
    let confirm!: (value: typeof point) => void;
    const prepare = vi.fn(() => new Promise<typeof point>((resolve) => { confirm = resolve; }));
    mocks.askBook.mockResolvedValue({...answer, read_frontier:point});
    const view = render(<AskTheBook {...props} bookmark={1} prepareReadContext={prepare} />);
    submit("Where did they meet?");
    expect(mocks.askBook).not.toHaveBeenCalled();
    // ReaderPage publishes the admitted chapter while confirming its exact text boundary.
    view.rerender(<AskTheBook {...props} bookmark={2} prepareReadContext={prepare} />);
    await act(async () => confirm(point));
    await screen.findByText(answer.claims[0].text);
    expect(mocks.askBook).toHaveBeenCalledWith("b", "Where did they meet?", 2, [], point, expect.any(AbortSignal));
    mocks.askBook.mockResolvedValue({...answer, read_frontier:{...point,id:"b".repeat(64)}});
    submit("And then?");
    await act(async () => confirm(point));
    await screen.findByText("I couldn’t get a reply. Try again?");
    expect(screen.getAllByText(answer.claims[0].text)).toHaveLength(1);
  });
  test("keeps this sitting's conversation while the book is open, and only the unsent draft in the browser", async () => {
    const sessionRef = { current: null as AskSession | null };
    const first = render(<AskTheBook {...props} sessionRef={sessionRef} />);
    submit("Where did they meet?");
    await screen.findByText(answer.claims[0].text);
    fireEvent.change(screen.getByLabelText("Message Mothli"), {
      target: { value: "And why?" },
    });
    first.unmount();
    // Nothing of the conversation is written to the browser's storage: only the draft.
    const kept = Object.keys(localStorage).map((key) => localStorage.getItem(key)).join("\n");
    expect(kept).toContain("And why?");
    expect(kept).not.toContain("Where did they meet?");
    expect(kept).not.toContain(answer.claims[0].text);
    const second = render(<AskTheBook {...props} bookmark={3} sessionRef={sessionRef} />);
    expect(
      (screen.getByLabelText("Message Mothli") as HTMLTextAreaElement).value,
    ).toBe("And why?");
    expect(screen.getByText("Where did they meet?")).toBeTruthy();
    fireEvent.submit(screen.getByLabelText("Message Mothli").closest("form")!);
    await screen.findByText("And why?");
    expect(mocks.askBook).toHaveBeenLastCalledWith("b", "And why?", 3, [
      { role: "user", content: "Where did they meet?", as_of_chapter: 2 },
      { role: "assistant", content: answer.claims[0].text, as_of_chapter: 2 },
    ], undefined, expect.any(AbortSignal));
    second.rerender(<AskTheBook {...props} sessionRef={sessionRef} readingPass={1} />);
    expect(screen.queryByText("Where did they meet?")).toBeNull();
    mocks.owner.mockReturnValue("other");
    second.rerender(<AskTheBook {...props} sessionRef={sessionRef} />);
    expect(screen.queryByText(answer.claims[0].text)).toBeNull();
    second.unmount();
    // A draft typed before the page was reloaded is still there; the conversation is not.
    mocks.owner.mockReturnValue("local");
    localStorage.setItem("litlet.pip.conversation:local:b:0", JSON.stringify({ question: "Half a thought" }));
    render(<AskTheBook {...props} />);
    expect((screen.getByLabelText("Message Mothli") as HTMLTextAreaElement).value).toBe("Half a thought");
  });

  test("reattaches to one pending request and discards late results after a reset", async () => {
    let finish!: (value: typeof answer) => void;
    mocks.askBook.mockImplementation(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    const sessionRef = { current: null as AskSession | null };
    const first = render(<AskTheBook {...props} sessionRef={sessionRef} />);
    submit("Where did they meet?");
    fireEvent.scroll(
      screen.getByRole("region", { name: "Book conversation" }),
      { target: { scrollTop: 120 } },
    );
    first.unmount();
    const second = render(<AskTheBook {...props} sessionRef={sessionRef} />);
    expect(
      screen.getByRole("region", { name: "Book conversation" }).scrollTop,
    ).toBe(120);
    expect(
      (screen.getByLabelText("Message Mothli") as HTMLTextAreaElement).disabled,
    ).toBe(true);
    expect(mocks.askBook).toHaveBeenCalledTimes(1);
    second.rerender(
      <AskTheBook {...props} sessionRef={sessionRef} readingPass={1} />,
    );
    await act(async () => finish(answer));
    expect(screen.queryByText(answer.claims[0].text)).toBeNull();
  });

  test("keeps the draft on failure and cancel; cancelled late results never replace a retry", async () => {
    mocks.askBook.mockRejectedValueOnce(new Error("network"));
    render(<AskTheBook {...props} />);
    submit("Where did they meet?");
    fireEvent.click(await screen.findByRole("button", { name: "Retry" }));
    await screen.findByText(answer.claims[0].text);
    let finish!: (value: typeof answer) => void;
    mocks.askBook.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    submit("Who else was there?");
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(
      (screen.getByLabelText("Message Mothli") as HTMLTextAreaElement).value,
    ).toBe("Who else was there?");
    await act(async () =>
      finish({
        ...answer,
        claims: [{ text: "A cancelled answer.", citation_ids: [1] }],
      }),
    );
    expect(screen.queryByText("A cancelled answer.")).toBeNull();
    fireEvent.click(
      screen.getByRole("button", { name: "Conversation options" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Clear" }));
    expect(screen.queryByText(answer.claims[0].text)).toBeNull();
    // The server keeps the conversation too; clearing the panel clears that copy.
    expect(mocks.clearConversation).toHaveBeenCalledWith("b");
    expect(
      (screen.getByLabelText("Message Mothli") as HTMLTextAreaElement).value,
    ).toBe("");
    mocks.askBook.mockRejectedValueOnce(
      new ApiError("book cost ceiling reached", 429),
    );
    submit("Who else was there?");
    expect(
      await screen.findByText(
        "Mothli is paused for this book: its usage limit has been reached.",
      ),
    ).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
    expect(
      (screen.getByLabelText("Message Mothli") as HTMLTextAreaElement).value,
    ).toBe("Who else was there?");
  });

  test("rejects future answers, and hides persisted later-book evidence when rewinding", async () => {
    const current = render(<AskTheBook {...props} />);
    submit("Where?");
    await screen.findByText(answer.claims[0].text);
    current.rerender(<AskTheBook {...props} bookmark={1} />);
    expect(screen.queryByText(answer.claims[0].text)).toBeNull();
    submit("Who?");
    await screen.findByRole("button", { name: "Retry" });
    expect(screen.queryByText(answer.claims[0].text)).toBeNull();
  });

  test("routes recap and person shortcuts into the same conversation without diagnostics", async () => {
    mocks.askBook.mockResolvedValue({
      ...answer,
      answer_type: "recap",
      recap: "Aldric arrived in the valley.",
      claims: [],
      citations: [],
    });
    render(<AskTheBook {...props} />);
    fireEvent.click(screen.getByRole("button", { name: "Catch me up" }));
    await screen.findByText("Aldric arrived in the valley.");
    expect(mocks.askBook).toHaveBeenCalledWith("b", "Catch me up", 2, [], undefined, expect.any(AbortSignal));
    fireEvent.click(screen.getByRole("button", { name: "Who was that?" }));
    fireEvent.click(await screen.findByRole("button", { name: "Aldric" }));
    expect(mocks.graph).toHaveBeenCalledWith("b",2);
    expect(mocks.catchMeUp).not.toHaveBeenCalled();
    expect(await screen.findByText("Remind me who Aldric is.")).toBeTruthy();
    expect(mocks.askBook).toHaveBeenLastCalledWith(
      "b",
      "Remind me who Aldric is.",
      2,
      expect.any(Array),
      undefined,
      expect.any(AbortSignal),
    );
    expect(screen.queryByText(/Provider|embeddings|terra/i)).toBeNull();
  });

  test("uses only the exact selected passage for its follow-up history", async () => {
    const selection = {
      text: "He left the key.",
      anchor: { atom: 3, cfi: "epubcfi(/6/4)", href: "c3.xhtml", title: "Key" },
    };
    mocks.selectionAction.mockResolvedValue({
      action: "explain",
      as_of_chapter: 2,
      insufficient_evidence: false,
      text: "He gives her a choice.",
      citation: {
        ...answer.citations[0],
        ordinal: 3,
        cfi: selection.anchor.cfi,
      },
      cost: null,
    });
    const view = render(
      <AskTheBook
        {...props}
        selection={selection}
        onClearSelection={vi.fn()}
      />,
    );
    submit("Why the key?");
    await screen.findByText("He gives her a choice.");
    submit("A choice about what?");
    expect(mocks.selectionAction.mock.calls[1][1].history).toEqual([
      { role: "user", content: "Why the key?", as_of_chapter: 2 },
      {
        role: "assistant",
        content: "He gives her a choice.",
        as_of_chapter: 2,
      },
    ]);
    await screen.findByText("A choice about what?");
    view.rerender(
      <AskTheBook
        {...props}
        selection={{ ...selection, text: "The kettle boiled." }}
      />,
    );
    submit("What does this mean?");
    expect(mocks.selectionAction.mock.calls[2][1].history).toEqual([]);
    expect(mocks.askBook).not.toHaveBeenCalled();
  });

  test("navigates verified sources and saves only on successful Notebook storage", async () => {
    const onSaveReply = vi.fn().mockImplementationOnce(() => {
      throw new Error("full");
    });
    const onNavigate = vi.fn();
    const { container } = render(
      <AskTheBook
        {...props}
        onSaveReply={onSaveReply}
        onNavigate={onNavigate}
      />,
    );
    submit("Where did they meet?");
    await screen.findByText(answer.claims[0].text);
    fireEvent.click(screen.getByText("Pages", { selector: "summary" }));
    fireEvent.click(screen.getByRole("button", { name: /The forge/ }));
    expect(onNavigate).toHaveBeenCalledWith(answer.citations[0]);
    fireEvent.click(
      screen.getByRole("button", { name: "Save reply to Notebook" }),
    );
    expect(
      screen.queryByRole("button", { name: "Saved to Notebook" }),
    ).toBeNull();
    fireEvent.click(
      screen.getByRole("button", { name: "Save reply to Notebook" }),
    );
    expect(onSaveReply).toHaveBeenLastCalledWith(
      expect.objectContaining({
        body: answer.claims[0].text,
        question: "Where did they meet?",
        citations: answer.citations,
      }),
    );
    expect(
      screen.getByRole("button", { name: "Saved to Notebook" }),
    ).toBeTruthy();
    expect(await axeAA(container)).toHaveNoViolations();
  });

  test("compact dialog closes on Escape and restores focus; desktop leaves reader keyboard access", () => {
    const opener = document.createElement("button");
    document.body.append(opener);
    opener.focus();
    const onClose = vi.fn();
    const view = render(<AskTheBook {...props} compact onClose={onClose} />);
    expect(screen.getByRole("dialog").getAttribute("aria-modal")).toBe("true");
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
    view.unmount();
    expect(document.activeElement).toBe(opener);
    opener.remove();
  });
});

// Clarifications are conversation, never an unsupported book fact or a saved AI finding.
test("shows a reaction clarification and retains it for the next turn without a source/save action", async () => {
  mocks.askBook.mockResolvedValueOnce({ ...answer, answer_type: "conversation", message: "Oof. Which part felt unfair to you?", claims: [], citations: [] });
  render(<AskTheBook {...props} onSaveReply={vi.fn()} />);
  submit("That was so unfair!");
  await screen.findByText("Oof. Which part felt unfair to you?");
  expect(screen.queryByRole("button", { name: "Save reply to Notebook" })).toBeNull();
  submit("When Mara was blamed for the key.");
  await screen.findByText(answer.claims[0].text);
  expect(mocks.askBook.mock.calls[1][3][1].content).toBe("Oof. Which part felt unfair to you?");
});

describe("chapters Mothli was not there for", () => {
  test("a reply that says so offers to count them, and one press does it", async () => {
    const readChapters = vi.fn().mockResolvedValue({});
    (mocks as Record<string, unknown>).readChapters = readChapters;
    const onChaptersRead = vi.fn();
    mocks.askBook.mockResolvedValueOnce({
      ...answer, claims: [{ text: "I wasn’t with you for chapters 1 and 2.", citation_ids: [] }], citations: [],
      offers: [{ book_id: "b", title: "The Forge", atoms: [1, 2], chapters: "chapters 1 and 2" }] });
    render(<AskTheBook {...props} onChaptersRead={onChaptersRead} />);
    submit("catch me up");
    fireEvent.click(await screen.findByRole("button", { name: "I’ve read chapters 1 and 2" }));
    await waitFor(() => expect(readChapters).toHaveBeenCalledWith("b", [1, 2], true));
    expect(await screen.findByText("Got it. I’ll count chapters 1 and 2 as read.")).toBeTruthy();
    expect(onChaptersRead).toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "I’ve read chapters 1 and 2" })).toBeNull();
    delete (mocks as Record<string, unknown>).readChapters;
  });

  test("an ordinary reply offers nothing", async () => {
    mocks.askBook.mockResolvedValueOnce(answer);
    render(<AskTheBook {...props} />);
    submit("Who met at the forge?");
    await screen.findByText(/Berenice met Aldric at the forge/);
    expect(screen.queryByRole("button", { name: /I’ve read/ })).toBeNull();
  });
});

// The conversation inside a book is the server's record. The server hides a turn once the text
// it was written from is no longer read; this panel must never show one from a copy of its own.
describe("the server's conversation", () => {
  const UNREACHABLE = "I can’t reach our earlier conversation right now, so I’m not showing it.";
  const streamedReply = (...sentences: string[]) => ({
    ...answer, style: "prose" as const, citations: [], read_frontier: point,
    claims: sentences.map((text) => ({ text, citation_ids: [] })),
  });

  // Catches: the panel showing what the browser kept instead of what the server returns.
  test("shows the server's turns on open and nothing from an old browser copy", async () => {
    const key = "litlet.pip.conversation:local:b:0";
    localStorage.setItem(key, JSON.stringify({
      question: "",
      exchanges: [{ id: "old", question: "A question from the old copy",
        answer: { ...answer, claims: [{ text: "A reply from the old copy.", citation_ids: [] }], citations: [] } }],
    }));
    mocks.bookConversation.mockResolvedValue(stored(["Who is Mitya?", "Fyodor’s eldest son."], ["And Ivan?", "The second son."]));
    render(<AskTheBook {...props} />);
    await screen.findByText("Fyodor’s eldest son.");
    expect(mocks.bookConversation).toHaveBeenCalledWith("b");
    expect(screen.getByText("Who is Mitya?")).toBeTruthy();
    expect(screen.getByText("And Ivan?")).toBeTruthy();
    expect(screen.getByText("The second son.")).toBeTruthy();
    expect(screen.queryByText("A question from the old copy")).toBeNull();
    expect(screen.queryByText("A reply from the old copy.")).toBeNull();
    // The old copy is deleted from the browser, not just ignored.
    expect(localStorage.getItem(key) ?? "").not.toContain("old copy");
  });

  // Catches: a question whose reply was not kept being shown bare, or with a made-up reply.
  // The server's copy does not say which fixed line the reader saw at the time.
  test("a stored question with no stored reply says plainly that there was no answer", async () => {
    mocks.bookConversation.mockResolvedValue(stored(["Who dies in the end?", null]));
    render(<AskTheBook {...props} onSaveReply={vi.fn()} />);
    await screen.findByText("Who dies in the end?");
    expect(screen.getByText("I didn’t have an answer for that from what you’d read.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Save reply to Notebook" })).toBeNull();
  });

  // Catches: turns surviving "read it fresh" from the panel's own memory. A new reading pass
  // shows only what the server returns for it.
  test("after a reset the earlier turns are gone from the screen", async () => {
    const sessionRef = { current: null as AskSession | null };
    mocks.bookConversation.mockResolvedValueOnce(stored(["Who is Mitya?", "Fyodor’s eldest son."]));
    const view = render(<AskTheBook {...props} sessionRef={sessionRef} />);
    await screen.findByText("Fyodor’s eldest son.");
    // ReaderPage after "read it fresh": the session is dropped and the pass number changes.
    mocks.bookConversation.mockResolvedValue(stored());
    sessionRef.current = null;
    view.rerender(<AskTheBook {...props} sessionRef={sessionRef} bookmark={0} readingPass={1} />);
    expect(screen.queryByText("Fyodor’s eldest son.")).toBeNull();
    await waitFor(() => expect(mocks.bookConversation).toHaveBeenCalledTimes(2));
    await screen.findByText(/Hi! I’m Mothli/);
    expect(screen.queryByText("Who is Mitya?")).toBeNull();
    expect(screen.queryByText("Fyodor’s eldest son.")).toBeNull();
  });

  // Catches: the fault 6w left open. "I haven't read this" keeps the reading pass, so nothing
  // used to remove the turns; they leave at once and come back only if the server returns them.
  test("“I haven’t read this” takes every turn off the screen until the server answers again", async () => {
    const sessionRef = { current: null as AskSession | null };
    const selection = { text: "He left the key.", anchor: { atom: 2, cfi: "epubcfi(/6/4)", href: "c2.xhtml", title: "Key" } };
    mocks.selectionAction.mockResolvedValue({ action: "explain", as_of_chapter: 2, insufficient_evidence: false,
      text: "He gives her a choice.", citation: { ...answer.citations[0], cfi: selection.anchor.cfi }, cost: null });
    mocks.bookConversation.mockResolvedValueOnce(stored(["About chapter one?", "Reply one."], ["About chapter two?", "Reply two."]));
    const view = render(<AskTheBook {...props} sessionRef={sessionRef} selection={selection} readVersion={0} />);
    await screen.findByText("Reply two.");
    submit("Why the key?");
    await screen.findByText("He gives her a choice."); // a reply the server does not keep
    let answerLoad!: (value: unknown) => void;
    mocks.bookConversation.mockImplementationOnce(() => new Promise((resolve) => { answerLoad = resolve; }));
    act(() => sessionRef.current!.hide()); // ReaderPage, the moment the chapter is marked not read
    expect(screen.queryByText("Reply one.")).toBeNull();
    expect(screen.queryByText("Reply two.")).toBeNull();
    expect(screen.queryByText("He gives her a choice.")).toBeNull();
    view.rerender(<AskTheBook {...props} sessionRef={sessionRef} selection={selection} readVersion={1} />);
    await waitFor(() => expect(mocks.bookConversation).toHaveBeenCalledTimes(2));
    expect(screen.queryByText("Reply one.")).toBeNull(); // still nothing while the server is asked
    await act(async () => answerLoad(stored(["About chapter one?", "Reply one."])));
    expect(screen.getByText("Reply one.")).toBeTruthy();
    expect(screen.queryByText("Reply two.")).toBeNull();
    expect(screen.queryByText("He gives her a choice.")).toBeNull();
  });

  // Catches: a reply still being written when the chapter is marked not read landing afterwards.
  test("a reply in flight when the turns are hidden is not shown when it lands", async () => {
    const sessionRef = { current: null as AskSession | null };
    let finish!: (value: unknown) => void;
    mocks.askBookStream.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
    render(<AskTheBook {...props} sessionRef={sessionRef} prepareReadContext={async () => point} />);
    submit("What did he say?");
    await waitFor(() => expect(mocks.askBookStream).toHaveBeenCalled());
    act(() => sessionRef.current!.hide());
    await act(async () => finish(streamedReply("Darcy called her tolerable.")));
    expect(screen.queryByText("Darcy called her tolerable.")).toBeNull();
    expect((screen.getByLabelText("Message Mothli") as HTMLTextAreaElement).value).toBe("What did he say?");
  });

  /** A stand-in server for streamed replies: it stores an exchange once the reply is done, unless
   * the request was stopped first, as `stream_read_prefix` does. */
  function streamingServer() {
    const turns: [string, string][] = [];
    const pending: { signal?: AbortSignal; question: string; resolve: (value: unknown) => void }[] = [];
    mocks.bookConversation.mockImplementation(async () => stored(...turns));
    mocks.askBookStream.mockImplementation((...args: unknown[]) => new Promise((resolve) => {
      pending.push({ question: args[1] as string, signal: args[5] as AbortSignal | undefined, resolve });
    }));
    return {
      pending,
      /** The server finishes every reply it is still writing. */
      finish: (reply: string) => {
        for (const { question, signal, resolve } of pending.splice(0)) {
          if (signal?.aborted) continue;
          turns.push([question, reply]);
          resolve(streamedReply(reply));
        }
      },
    };
  }

  // Catches: Cancel only taking the reply off the screen while the request runs on (audit 6z).
  test("Cancel stops the request for the reply being written", async () => {
    const server = streamingServer();
    render(<AskTheBook {...props} prepareReadContext={async () => point} />);
    submit("What did he say?");
    await waitFor(() => expect(server.pending).toHaveLength(1));
    const signal = server.pending[0].signal;
    expect(signal?.aborted).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(signal?.aborted).toBe(true);
  });

  // Catches: the fault in 6z's list. The reply the reader cancelled was finished and stored by
  // the server, and appeared the next time the conversation was loaded.
  test("a cancelled exchange does not come back at the next load", async () => {
    const server = streamingServer();
    const view = render(<AskTheBook {...props} prepareReadContext={async () => point} readVersion={0} />);
    submit("What did he say?");
    await waitFor(() => expect(server.pending).toHaveLength(1));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await act(async () => server.finish("Darcy called her tolerable."));
    view.rerender(<AskTheBook {...props} prepareReadContext={async () => point} readVersion={1} />);
    await waitFor(() => expect(mocks.bookConversation).toHaveBeenCalledTimes(2));
    await act(async () => {});
    expect(screen.queryByText("Darcy called her tolerable.")).toBeNull();
    expect(screen.queryByText("What did he say?", { selector: ".mothli-question" })).toBeNull();
    // A reply that is not cancelled is stored and comes back, as before.
    submit("And then?");
    await waitFor(() => expect(server.pending).toHaveLength(1));
    await act(async () => server.finish("She laughed."));
    await screen.findByText("She laughed.");
    view.rerender(<AskTheBook {...props} prepareReadContext={async () => point} readVersion={2} />);
    await waitFor(() => expect(mocks.bookConversation).toHaveBeenCalledTimes(3));
    expect(screen.getAllByText("She laughed.")).toHaveLength(1);
  });

  // Catches: the other ways a reply in flight is dropped from the screen leaving its request
  // running: "I haven't read this" (hide) and a new reading pass.
  test("a reply dropped by “I haven’t read this” or a new reading pass is stopped too", async () => {
    const server = streamingServer();
    const sessionRef = { current: null as AskSession | null };
    const view = render(<AskTheBook {...props} sessionRef={sessionRef} prepareReadContext={async () => point} />);
    submit("What did he say?");
    await waitFor(() => expect(server.pending).toHaveLength(1));
    const hidden = server.pending[0].signal;
    act(() => sessionRef.current!.hide());
    expect(hidden?.aborted).toBe(true);
    await act(async () => server.finish("Darcy called her tolerable."));
    submit("And then?");
    await waitFor(() => expect(server.pending).toHaveLength(1));
    const reset = server.pending[0].signal;
    view.rerender(<AskTheBook {...props} sessionRef={sessionRef} prepareReadContext={async () => point} bookmark={0} readingPass={1} />);
    expect(reset?.aborted).toBe(true);
  });

  // Catches: the reply shown twice (the panel's copy and the server's), or drawn again, when
  // the server's conversation is loaded after it arrived.
  test("a streamed reply appears once and is not duplicated or redrawn by the reload", async () => {
    mocks.bookConversation.mockResolvedValueOnce(stored(["Who is Mitya?", "Fyodor’s eldest son."]));
    let say!: (text: string) => void;
    let finish!: (value: unknown) => void;
    mocks.askBookStream.mockImplementationOnce((...args: unknown[]) => {
      say = args[4] as (text: string) => void;
      return new Promise((resolve) => { finish = resolve; });
    });
    const view = render(<AskTheBook {...props} prepareReadContext={async () => point} readVersion={0} />);
    await screen.findByText("Fyodor’s eldest son.");
    submit("What did he say?");
    await waitFor(() => expect(mocks.askBookStream).toHaveBeenCalled());
    act(() => say("Darcy called her tolerable."));
    await screen.findByText("Darcy called her tolerable.");
    await act(async () => finish(streamedReply("Darcy called her tolerable.", "Ouch.")));
    const shown = await screen.findByText("Darcy called her tolerable. Ouch.");
    // The server now has it too.
    mocks.bookConversation.mockResolvedValueOnce(stored(
      ["Who is Mitya?", "Fyodor’s eldest son."], ["What did he say?", "Darcy called her tolerable. Ouch."]));
    view.rerender(<AskTheBook {...props} prepareReadContext={async () => point} readVersion={1} />);
    await waitFor(() => expect(mocks.bookConversation).toHaveBeenCalledTimes(2));
    await act(async () => {});
    expect(screen.getAllByText("Darcy called her tolerable. Ouch.")).toHaveLength(1);
    expect(screen.getAllByText("What did he say?")).toHaveLength(1);
    expect(screen.getAllByText("Fyodor’s eldest son.")).toHaveLength(1);
    expect(screen.getByText("Darcy called her tolerable. Ouch.")).toBe(shown); // the same element: not drawn again
    const order = screen.getAllByRole("region")
      .filter((node) => /^Question/.test(node.getAttribute("aria-label") ?? ""))
      .map((node) => node.textContent ?? "");
    expect(order).toHaveLength(2);
    expect(order[0]).toContain("Who is Mitya?");
    expect(order[1]).toContain("What did he say?");
  });

  // Catches: the reply the reader has just watched arrive vanishing because the server's copy
  // was asked for before the server had stored it (it stores a turn after its last sentence);
  // and the opposite, the panel's copy outliving the server's word for good.
  test("a reply that has just arrived stays until the server has had time to store it, and no longer", async () => {
    const now = vi.spyOn(Date, "now");
    try {
      now.mockReturnValue(1_000_000);
      mocks.askBookStream.mockResolvedValueOnce(streamedReply("Darcy called her tolerable."));
      const view = render(<AskTheBook {...props} prepareReadContext={async () => point} readVersion={0} />);
      await waitFor(() => expect(mocks.bookConversation).toHaveBeenCalledTimes(1));
      submit("What did he say?");
      await screen.findByText("Darcy called her tolerable.");
      now.mockReturnValue(1_001_000); // a second later the server's copy does not have it yet
      view.rerender(<AskTheBook {...props} prepareReadContext={async () => point} readVersion={1} />);
      await waitFor(() => expect(mocks.bookConversation).toHaveBeenCalledTimes(2));
      await act(async () => {});
      expect(screen.getByText("Darcy called her tolerable.")).toBeTruthy();
      now.mockReturnValue(1_060_000); // a minute later it is still not there: the server's word stands
      view.rerender(<AskTheBook {...props} prepareReadContext={async () => point} readVersion={2} />);
      await waitFor(() => expect(mocks.bookConversation).toHaveBeenCalledTimes(3));
      await waitFor(() => expect(screen.queryByText("Darcy called her tolerable.")).toBeNull());
    } finally {
      now.mockRestore();
    }
  });

  // Catches: a turn the server stopped returning (hidden or cleared from another tab) staying
  // on a panel that is already open.
  test("a turn the server no longer returns leaves the screen at the next load", async () => {
    mocks.bookConversation.mockResolvedValueOnce(stored(["About chapter one?", "Reply one."], ["About chapter two?", "Reply two."]));
    const view = render(<AskTheBook {...props} readVersion={0} />);
    await screen.findByText("Reply two.");
    mocks.bookConversation.mockResolvedValueOnce(stored(["About chapter one?", "Reply one."]));
    view.rerender(<AskTheBook {...props} readVersion={1} />);
    await waitFor(() => expect(screen.queryByText("Reply two.")).toBeNull());
    expect(screen.getByText("Reply one.")).toBeTruthy();
  });

  // Catches: an old copy left on screen because the server could not be asked.
  test("a failed load shows no earlier turns, says so, and offers to try again", async () => {
    const sessionRef = { current: null as AskSession | null };
    mocks.bookConversation.mockResolvedValueOnce(stored(["Who is Mitya?", "Fyodor’s eldest son."]));
    const first = render(<AskTheBook {...props} sessionRef={sessionRef} />);
    await screen.findByText("Fyodor’s eldest son.");
    first.unmount();
    // The panel is opened again and the server cannot be reached.
    mocks.bookConversation.mockRejectedValueOnce(new Error("network"));
    render(<AskTheBook {...props} sessionRef={sessionRef} />);
    await screen.findByText(UNREACHABLE);
    expect(screen.queryByText("Who is Mitya?")).toBeNull();
    expect(screen.queryByText("Fyodor’s eldest son.")).toBeNull();
    mocks.bookConversation.mockResolvedValueOnce(stored(["Who is Mitya?", "Fyodor’s eldest son."]));
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    await screen.findByText("Fyodor’s eldest son.");
    expect(screen.queryByText(UNREACHABLE)).toBeNull();
  });

  // Catches: a failed first load falling back to anything the browser has.
  test("a failed first load shows nothing from the browser either", async () => {
    localStorage.setItem("litlet.pip.conversation:local:b:0", JSON.stringify({
      question: "",
      exchanges: [{ id: "old", question: "A question from the old copy", answer: { ...answer, citations: [] } }],
    }));
    mocks.bookConversation.mockRejectedValue(new ApiError("reading context changed; refresh before continuing", 409));
    render(<AskTheBook {...props} />);
    await screen.findByText(UNREACHABLE);
    expect(screen.queryByText("A question from the old copy")).toBeNull();
    expect(screen.queryByText(answer.claims[0].text)).toBeNull();
  });

  // Catches: a server with no stored conversation (hosted mode has none) treated as a failure.
  test("a server that keeps no conversation for books gives an empty conversation, not an error", async () => {
    mocks.bookConversation.mockRejectedValue(new ApiError("Not Found", 404));
    render(<AskTheBook {...props} />);
    await screen.findByText(/Hi! I’ve been reading along with you/);
    expect(screen.queryByText(UNREACHABLE)).toBeNull();
  });

  // Catches: "Clear" only emptying the screen. The server is asked again afterwards, and if it
  // still has the turns (the clear did not get through) the reader sees them and is told.
  test("clearing asks the server again, and a clear that did not get through is not hidden", async () => {
    mocks.bookConversation.mockResolvedValue(stored(["Who is Mitya?", "Fyodor’s eldest son."]));
    render(<AskTheBook {...props} />);
    await screen.findByText("Fyodor’s eldest son.");
    mocks.bookConversation.mockResolvedValueOnce(stored());
    fireEvent.click(screen.getByRole("button", { name: "Conversation options" }));
    fireEvent.click(screen.getByRole("button", { name: "Clear" }));
    expect(screen.queryByText("Fyodor’s eldest son.")).toBeNull();
    await waitFor(() => expect(mocks.bookConversation).toHaveBeenCalledTimes(2));
    expect(mocks.clearConversation).toHaveBeenCalledWith("b");
    await act(async () => {});
    expect(screen.queryByText("Fyodor’s eldest son.")).toBeNull();
    // Now a clear the server never received.
    mocks.clearConversation.mockRejectedValueOnce(new Error("network"));
    fireEvent.click(screen.getByRole("button", { name: "Conversation options" }));
    fireEvent.click(screen.getByRole("button", { name: "Clear" }));
    await screen.findByText("I couldn’t clear our conversation just now, so it is still here.");
    expect(screen.getByText("Fyodor’s eldest son.")).toBeTruthy();
  });
});

// Hosted mode has no story memory yet (no extraction) and no reader-key routes: the server answers
// 404 for the cast, the chapter notes, the closeout, the names and passage help. The reader is
// told something true, and "Tell me" still gets an answer from the chat.
describe("a server without story memory (hosted mode)", () => {
  const missing = () => new ApiError("Not Found", 404);

  test("“Tell me” after a chapter asks the chat when there is no closeout", async () => {
    mocks.chapterCloseout.mockRejectedValue(missing());
    mocks.askBookStream.mockImplementation(async (_id, _q, _b, _p, onSentence) => {
      onSentence("Tobias walked the middle rows.");
      return { ...answer, claims: [{ text: "Tobias walked the middle rows.", citation_ids: [] }], citations: [], style: "prose" };
    });
    render(<AskTheBook {...props} prepareReadContext={vi.fn(async () => point)}
      seed={{ id: "closeout-2", kind: "closeout", chapter: 2, label: "After The Keeper" }} />);
    await screen.findByText("What did you notice in The Keeper?");
    await waitFor(() => expect(mocks.askBookStream).toHaveBeenCalled());
    expect(mocks.askBookStream.mock.calls[0][1]).toBe("What did you notice in The Keeper?");
    expect(screen.queryByText("I couldn’t gather my thoughts on that chapter. Try again?")).toBeNull();
  });

  test("the cast and the story say there is no memory instead of “try again”", async () => {
    mocks.graph.mockRejectedValue(missing());
    mocks.notes.mockRejectedValue(missing());
    render(<AskTheBook {...props} />);
    fireEvent.click(screen.getByRole("button", { name: "Cast" }));
    await screen.findByText(/I don’t keep a list of who’s who for this book yet/);
    fireEvent.click(screen.getByRole("button", { name: "Story so far" }));
    await screen.findByText(/I don’t keep chapter-by-chapter notes for this book yet/);
    expect(screen.queryByText(/Try again in a moment/)).toBeNull();
  });

  test("“Who was that?” and a question about a picked passage say what is missing", async () => {
    mocks.graph.mockRejectedValue(missing());
    mocks.selectionAction.mockRejectedValue(missing());
    render(<AskTheBook {...props} selection={{ text: "Wren counted the apples.", anchor: { atom: 2, cfi: "cfi" } }} />);
    fireEvent.click(screen.getByRole("button", { name: "Who was that?" }));
    await screen.findByText(/I don’t keep a list of names for this book yet/);
    submit("What does this mean?");
    await screen.findByText(/I can’t help with a picked passage here yet/);
    expect(screen.queryByText("I couldn’t get a reply. Try again?")).toBeNull();
  });

  test("a closeout that is not ready yet still says so (local mode, unchanged)", async () => {
    mocks.chapterCloseout.mockRejectedValue(new ApiError("not yet", 500));
    render(<AskTheBook {...props} seed={{ id: "closeout-3", kind: "closeout", chapter: 2, label: "After The Keeper" }} />);
    await screen.findByText("I couldn’t gather my thoughts on that chapter. Try again?");
    expect(mocks.askBookStream).not.toHaveBeenCalled();
  });
});
