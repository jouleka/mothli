import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, test, vi } from "vitest";
import { axeAA } from "../test-a11y";
const mocks = vi.hoisted(() => ({
  askBook: vi.fn(),
  selectionAction: vi.fn(),
  catchMeUp: vi.fn(),
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
function submit(value: string) {
  fireEvent.change(screen.getByLabelText("Message Pip"), { target: { value } });
  fireEvent.submit(screen.getByLabelText("Message Pip").closest("form")!);
}
beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  mocks.owner.mockReturnValue("local");
  mocks.askBook.mockResolvedValue(answer);
  mocks.catchMeUp.mockResolvedValue({
    as_of_chapter: 2,
    cast: [{ name: "Aldric", entity_id: 1 }],
  });
});

describe("Pip conversation", () => {
  test("persists conversation and draft per book, owner and reading pass; advancing retains useful context", async () => {
    const first = render(<AskTheBook {...props} />);
    submit("Where did they meet?");
    await screen.findByText(answer.claims[0].text);
    fireEvent.change(screen.getByLabelText("Message Pip"), {
      target: { value: "And why?" },
    });
    first.unmount();
    const second = render(<AskTheBook {...props} bookmark={3} />);
    expect(
      (screen.getByLabelText("Message Pip") as HTMLTextAreaElement).value,
    ).toBe("And why?");
    expect(screen.getByText("Where did they meet?")).toBeTruthy();
    fireEvent.submit(screen.getByLabelText("Message Pip").closest("form")!);
    await screen.findByText("And why?");
    expect(mocks.askBook).toHaveBeenLastCalledWith("b", "And why?", 3, [
      { role: "user", content: "Where did they meet?", as_of_chapter: 2 },
      { role: "assistant", content: answer.claims[0].text, as_of_chapter: 2 },
    ]);
    second.rerender(<AskTheBook {...props} readingPass={1} />);
    expect(screen.queryByText("Where did they meet?")).toBeNull();
    mocks.owner.mockReturnValue("other");
    second.rerender(<AskTheBook {...props} />);
    expect(screen.queryByText(answer.claims[0].text)).toBeNull();
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
      (screen.getByLabelText("Message Pip") as HTMLTextAreaElement).disabled,
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
      (screen.getByLabelText("Message Pip") as HTMLTextAreaElement).value,
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
    expect(
      (screen.getByLabelText("Message Pip") as HTMLTextAreaElement).value,
    ).toBe("");
    mocks.askBook.mockRejectedValueOnce(
      new ApiError("book cost ceiling reached", 429),
    );
    submit("Who else was there?");
    expect(
      await screen.findByText(
        "Pip is paused for this book: its usage limit has been reached.",
      ),
    ).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
    expect(
      (screen.getByLabelText("Message Pip") as HTMLTextAreaElement).value,
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
    expect(mocks.askBook).toHaveBeenCalledWith("b", "Catch me up", 2, []);
    fireEvent.click(screen.getByRole("button", { name: "Who was that?" }));
    fireEvent.click(await screen.findByRole("button", { name: "Aldric" }));
    expect(await screen.findByText("Remind me who Aldric is.")).toBeTruthy();
    expect(mocks.askBook).toHaveBeenLastCalledWith(
      "b",
      "Remind me who Aldric is.",
      2,
      expect.any(Array),
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
