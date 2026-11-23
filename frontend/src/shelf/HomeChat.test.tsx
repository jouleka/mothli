import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { axeAA } from "../test-a11y";

const mocks = vi.hoisted(() => ({
  homeConversation: vi.fn(), askHome: vi.fn(), clearHome: vi.fn(), readChapters: vi.fn(), addClassic: vi.fn(),
}));
vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: mocks,
}));
import { ApiError, type HomeTurn } from "../api";
import { HomeChat } from "./HomeChat";

beforeEach(() => {
  mocks.homeConversation.mockReset().mockResolvedValue({ turns: [] });
  mocks.askHome.mockReset();
  mocks.clearHome.mockReset().mockResolvedValue(undefined);
  mocks.readChapters.mockReset().mockResolvedValue(undefined);
  mocks.addClassic.mockReset().mockResolvedValue({ book_id: "bknew" });
});
afterEach(() => {
  vi.useRealTimers();
});

/** `count` saved exchanges: "Question 1" / "Answer 1", ... */
const history = (count: number): HomeTurn[] =>
  Array.from({ length: count }, (_, index) => [
    { role: "user" as const, content: `Question ${index + 1}` },
    { role: "assistant" as const, content: `Answer ${index + 1}` },
  ]).flat();

/** jsdom has no layout: give the conversation area a size and a scroll position we control. */
function fakeScrollGeometry(node: HTMLElement, { height, view }: { height: number; view: number }) {
  let top = 0;
  const geometry = { height };
  Object.defineProperty(node, "scrollHeight", { configurable: true, get: () => geometry.height });
  Object.defineProperty(node, "clientHeight", { configurable: true, get: () => view });
  Object.defineProperty(node, "scrollTop", {
    configurable: true,
    get: () => top,
    set: (value: number) => { top = Math.max(0, Math.min(value, geometry.height - view)); },
  });
  return geometry;
}

const box = () => screen.getByLabelText("Ask Mothli about your books") as HTMLTextAreaElement;
const ask = (text: string) => {
  fireEvent.change(box(), { target: { value: text } });
  fireEvent.click(screen.getByRole("button", { name: "Ask" }));
};

describe("HomeChat", () => {
  test("is not shown where the server has no home conversation", async () => {
    mocks.homeConversation.mockRejectedValue(new ApiError("not enabled", 404));
    const { container } = render(<HomeChat />);
    await waitFor(() => expect(mocks.homeConversation).toHaveBeenCalled());
    expect(container.textContent).toBe("");
  });

  test("an empty conversation invites a question in Mothli's voice and offers a first question", async () => {
    let finish: (value: unknown) => void = () => {};
    mocks.askHome.mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
    const { container } = render(<HomeChat />);
    expect(await screen.findByText(/Ask me about any book on your shelf/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Clear conversation" })).toBeNull();
    expect(await axeAA(container)).toHaveNoViolations();
    fireEvent.click(screen.getByRole("button", { name: "Where did I leave off?" }));
    expect(mocks.askHome.mock.calls[0][0]).toBe("Where did I leave off?");
    expect(screen.queryByRole("button", { name: "Where did I leave off?" })).toBeNull();
    await act(async () => finish({ insufficient_evidence: false, claims: [{ text: "Chapter four.", citation_ids: [] }] }));
    const thread = screen.getByRole("region", { name: "Conversation with Mothli" });
    expect(await within(thread).findByText("Chapter four.")).toBeTruthy();
  });

  test("with nothing on the shelf, the first questions and the line above them fit a new reader", async () => {
    // Break caught (6al): a reader with an empty shelf was offered "Where did I leave off?".
    mocks.askHome.mockResolvedValue({ insufficient_evidence: false, claims: [{ text: "Hi, I’m Mothli.", citation_ids: [] }] });
    const { container } = render(<HomeChat shelfEmpty />);
    expect(await screen.findByText(
      "Nothing on your shelf yet? Ask me what I can do, or for a good first book to start with.")).toBeTruthy();
    expect(screen.queryByText(/Ask me about any book on your shelf/)).toBeNull();
    expect(screen.queryByRole("button", { name: "Where did I leave off?" })).toBeNull();
    expect(screen.queryByRole("button", { name: "What should I read tonight?" })).toBeNull();
    expect(screen.getByRole("button", { name: "What should I read first?" })).toBeTruthy();
    expect(await axeAA(container)).toHaveNoViolations();
    fireEvent.click(screen.getByRole("button", { name: "What can you do?" }));
    expect(mocks.askHome.mock.calls[0][0]).toBe("What can you do?");
    const thread = await screen.findByRole("region", { name: "Conversation with Mothli" });
    expect(await within(thread).findByText("Hi, I’m Mothli.")).toBeTruthy();
  });

  test("with books on the shelf, the first questions are as before", async () => {
    render(<HomeChat shelfEmpty={false} />);
    expect(await screen.findByText(
      "Ask me about any book on your shelf: where you left off, who someone was, what to read next.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Where did I leave off?" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "What should I read tonight?" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "What can you do?" })).toBeNull();
  });

  test("shows the earlier conversation, then each checked sentence of a new reply as it arrives", async () => {
    mocks.homeConversation.mockResolvedValue({ turns: [
      { role: "user", content: "Where am I?", created_at: "2026-11-15T10:00:00Z" },
      { role: "assistant", content: "Four chapters into Karamazov.", created_at: "2026-11-15T10:00:02Z" }] });
    let finish: (value: unknown) => void = () => {};
    let sentence: (text: string) => void = () => {};
    mocks.askHome.mockImplementation((_question: string, onSentence: (text: string) => void) => {
      sentence = onSentence;
      return new Promise((resolve) => { finish = resolve; });
    });
    const { container } = render(<HomeChat />);
    expect(await screen.findByText("Four chapters into Karamazov.")).toBeTruthy();
    ask("What next?");
    const thread = screen.getByRole("region", { name: "Conversation with Mothli" });
    // The question shows at once, and Mothli says it is thinking before any sentence exists.
    expect(within(thread).getByText("What next?")).toBeTruthy();
    expect(within(thread).getByText(/Thinking it over/)).toBeTruthy();
    expect(mocks.askHome.mock.calls[0][0]).toBe("What next?");
    act(() => sentence("I’d pick up Karamazov tonight."));
    expect(within(thread).getByText("I’d pick up Karamazov tonight.")).toBeTruthy();
    // A screen reader hears only the new sentence, never the whole conversation again.
    expect(thread.querySelector("[aria-live]")).toBeNull();
    const spoken = screen.getByRole("log", { name: "Mothli’s reply as it arrives" });
    expect(spoken.textContent).toContain("I’d pick up Karamazov tonight.");
    expect(spoken.textContent).not.toContain("Four chapters into Karamazov.");
    await act(async () => finish({ insufficient_evidence: false, claims: [
      { text: "I’d pick up Karamazov tonight.", citation_ids: [] },
      { text: "You’re one chapter from the end.", citation_ids: [] }] }));
    expect(await within(thread).findByText(/You’re one chapter from the end\./)).toBeTruthy();
    // The sentence that only came with the finished reply is spoken too, once.
    expect(spoken.textContent).toContain("You’re one chapter from the end.");
    expect(spoken.textContent?.match(/I’d pick up Karamazov tonight\./g)).toHaveLength(1);
    expect(within(thread).queryByText(/Thinking it over/)).toBeNull();
    expect(box().value).toBe("");
    expect(await axeAA(container)).toHaveNoViolations();
  });

  // Catches: leaving the home screen while a reply is written dropped it from the screen but let
  // the request run on, so the server stored a reply the reader never saw (as in audit 6z).
  test("leaving the home screen while a reply is written stops its request", async () => {
    mocks.askHome.mockImplementation(() => new Promise(() => {}));
    const view = render(<HomeChat />);
    await screen.findByLabelText("Ask Mothli about your books");
    ask("Who is Alyosha?");
    const signal = mocks.askHome.mock.calls[0][2] as AbortSignal | undefined;
    expect(signal?.aborted).toBe(false);
    view.unmount();
    expect(signal?.aborted).toBe(true);
  });

  test("a long wait before the first sentence is acknowledged calmly", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    mocks.askHome.mockImplementation(() => new Promise(() => {}));
    render(<HomeChat />);
    await screen.findByLabelText("Ask Mothli about your books");
    ask("Who is Alyosha?");
    expect(screen.getByText(/Thinking it over/)).toBeTruthy();
    act(() => { vi.advanceTimersByTime(7000); });
    expect(screen.getByText(/Still thinking/)).toBeTruthy();
  });

  // Catches: the hosted allowance or a reply still in flight shown as a failure.
  test("at the allowance, says the server's line calmly instead of an error", async () => {
    const line = "Mothli has talked a lot with you today and is resting until tomorrow. Your book and your place are right here.";
    mocks.askHome.mockRejectedValueOnce(new ApiError(line, 429, "allowance_reached"));
    render(<HomeChat />);
    await screen.findByLabelText("Ask Mothli about your books");
    ask("What next?");
    // On the shelf it speaks of the books, not one book (L6 of the plan walk); only the words.
    await waitFor(() => expect(screen.getByRole("status").textContent).toBe(
      "Mothli has talked a lot with you today and is resting until tomorrow. Your books and your place in each are right here."));
    expect(screen.queryByRole("alert")).toBeNull();
    expect(box().value).toBe("What next?");
    // A refusal that came without its words: the app's own line, for the shelf too.
    mocks.askHome.mockRejectedValueOnce(new ApiError("", 503, "allowance_reached"));
    ask("What next?");
    await waitFor(() => expect(screen.getByRole("status").textContent).toBe(
      "We’ve talked a lot today — let’s pick this up tomorrow. Your books and your place in each are right here."));
    mocks.askHome.mockRejectedValueOnce(new ApiError("Mothli is still answering you.", 429, "companion_busy"));
    ask("What next?");
    await waitFor(() => expect(screen.getByRole("status").textContent)
      .toBe("Give me a second — I’m still on your last question. Ask again in a moment."));
    expect(screen.queryByRole("alert")).toBeNull();
  });

  test("a reply with nothing shown says so, a failure keeps the question, and clear empties both sides", async () => {
    mocks.askHome.mockResolvedValueOnce({ insufficient_evidence: true, claims: [] });
    render(<HomeChat />);
    await screen.findByLabelText("Ask Mothli about your books");
    ask("Who dies?");
    const thread = screen.getByRole("region", { name: "Conversation with Mothli" });
    expect(await within(thread).findByText(/haven’t got there yet/i)).toBeTruthy();
    mocks.askHome.mockRejectedValueOnce(new ApiError("Jev review unavailable", 503));
    ask("And then?");
    expect((await screen.findByRole("alert")).textContent).toMatch(/can’t check its reply/);
    expect(box().value).toBe("And then?");
    fireEvent.click(screen.getByRole("button", { name: "Clear conversation" }));
    // Clearing a whole conversation is asked once, and can be called off.
    fireEvent.click(screen.getByRole("button", { name: "Keep it" }));
    expect(screen.getByText("Who dies?")).toBeTruthy();
    expect(mocks.clearHome).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Clear conversation" }));
    fireEvent.click(screen.getByRole("button", { name: "Clear it" }));
    await waitFor(() => expect(mocks.clearHome).toHaveBeenCalled());
    expect(screen.queryByText("Who dies?")).toBeNull();
    expect(screen.getByText(/Ask me about any book on your shelf/)).toBeTruthy();
    expect(document.activeElement).toBe(box());
  });

  test("a long conversation shows the latest exchanges and lets the reader reach earlier ones", async () => {
    mocks.homeConversation.mockResolvedValue({ turns: history(20) });
    const { container } = render(<HomeChat />);
    const thread = await screen.findByRole("region", { name: "Conversation with Mothli" });
    expect(thread.tabIndex).toBe(0); // scrollable by keyboard
    const exchanges = () => within(thread).getAllByRole("listitem");
    expect(exchanges()).toHaveLength(6);
    expect(exchanges()[0].textContent).toContain("Question 15");
    expect(within(thread).queryByText("Question 14")).toBeNull();
    expect(within(thread).getByText("Answer 20")).toBeTruthy();
    fireEvent.click(within(thread).getByRole("button", { name: "Show earlier (14 more)" }));
    expect(exchanges()).toHaveLength(16);
    // Focus lands on the nearest earlier exchange, so reading carries on upwards from there.
    expect(document.activeElement?.textContent).toContain("Question 14");
    fireEvent.click(within(thread).getByRole("button", { name: "Show earlier (4 more)" }));
    expect(exchanges()).toHaveLength(20);
    expect(within(thread).queryByRole("button", { name: /Show earlier/ })).toBeNull();
    expect(await axeAA(container)).toHaveNoViolations();
  });

  test("the newest reply stays in view as it arrives, unless the reader has scrolled back to read", async () => {
    mocks.homeConversation.mockResolvedValue({ turns: history(3) });
    let sentence: (text: string) => void = () => {};
    let finish: (value: unknown) => void = () => {};
    mocks.askHome.mockImplementation((_question: string, onSentence: (text: string) => void) => {
      sentence = onSentence;
      return new Promise((resolve) => { finish = resolve; });
    });
    render(<HomeChat />);
    const thread = await screen.findByRole("region", { name: "Conversation with Mothli" });
    const geometry = fakeScrollGeometry(thread, { height: 1200, view: 400 });

    ask("Anything else?");
    geometry.height = 1400;
    act(() => sentence("First thought."));
    expect(thread.scrollTop).toBe(1000); // followed to the bottom
    expect(screen.queryByRole("button", { name: "Jump to the newest reply" })).toBeNull();

    // The reader scrolls up to reread: arriving sentences no longer pull them down.
    thread.scrollTop = 200;
    fireEvent.scroll(thread);
    geometry.height = 1600;
    act(() => sentence("Second thought."));
    expect(thread.scrollTop).toBe(200);
    fireEvent.click(screen.getByRole("button", { name: "Jump to the newest reply" }));
    expect(thread.scrollTop).toBe(1200);
    expect(document.activeElement?.textContent).toContain("Second thought.");
    fireEvent.scroll(thread);
    expect(screen.queryByRole("button", { name: "Jump to the newest reply" })).toBeNull();
    await act(async () => finish({ insufficient_evidence: false, claims: [{ text: "First thought.", citation_ids: [] }] }));
  });

  describe("offers to count skipped chapters as read", () => {
    const pride = { book_id: "bk1", title: "Pride and Prejudice", atoms: [1, 2], chapters: "chapters 1 and 2" };
    const answerWith = (offers?: unknown[]) => ({
      insufficient_evidence: false,
      claims: [{ text: "I wasn’t with you for chapters 1 and 2 of Pride and Prejudice.", citation_ids: [] }],
      ...(offers ? { offers } : {}),
    });

    test("an offer sits under the reply it came with; accepting it counts the chapters and says so", async () => {
      mocks.homeConversation.mockResolvedValue({ turns: history(1) });
      mocks.askHome.mockResolvedValueOnce(answerWith([pride]));
      let done: (value: unknown) => void = () => {};
      mocks.readChapters.mockImplementationOnce(() => new Promise((resolve) => { done = resolve; }));
      const { container } = render(<HomeChat />);
      await screen.findByText("Answer 1");
      ask("Where was I?");
      const thread = screen.getByRole("region", { name: "Conversation with Mothli" });
      const offer = await within(thread).findByRole("button", { name: "I’ve read chapters 1 and 2 of Pride and Prejudice" });
      // It belongs to the reply it came with, not to the earlier exchange.
      const items = within(thread).getAllByRole("listitem");
      expect(items[items.length - 1].contains(offer)).toBe(true);
      expect(items[0].querySelector("button")).toBeNull();
      expect(await axeAA(container)).toHaveNoViolations();
      fireEvent.click(offer);
      expect(mocks.readChapters).toHaveBeenCalledWith("bk1", [1, 2], true);
      expect((offer as HTMLButtonElement).disabled).toBe(true);
      await act(async () => done(undefined));
      expect(within(thread).queryByRole("button", { name: /I’ve read/ })).toBeNull();
      const confirmed = "Got it. I’ll count chapters 1 and 2 of Pride and Prejudice as read.";
      expect(within(thread).getByText(confirmed)).toBeTruthy();
      expect(screen.getByRole("log", { name: "Mothli’s reply as it arrives" }).textContent).toContain(confirmed);
    });

    test("if counting the chapters fails, Mothli says so calmly and the offer comes back", async () => {
      mocks.askHome.mockResolvedValueOnce(answerWith([pride]));
      mocks.readChapters.mockRejectedValueOnce(new ApiError("nope", 500));
      render(<HomeChat />);
      await screen.findByLabelText("Ask Mothli about your books");
      ask("Where was I?");
      const thread = screen.getByRole("region", { name: "Conversation with Mothli" });
      fireEvent.click(await within(thread).findByRole("button", { name: "I’ve read chapters 1 and 2 of Pride and Prejudice" }));
      expect(await within(thread).findByText(/couldn’t note that/)).toBeTruthy();
      const again = within(thread).getByRole("button", { name: "I’ve read chapters 1 and 2 of Pride and Prejudice" }) as HTMLButtonElement;
      expect(again.disabled).toBe(false);
      expect(within(thread).queryByText(/Got it/)).toBeNull();
    });

    test("a reply without offers shows nothing extra", async () => {
      mocks.askHome.mockResolvedValueOnce(answerWith());
      render(<HomeChat />);
      await screen.findByLabelText("Ask Mothli about your books");
      ask("Where was I?");
      const thread = screen.getByRole("region", { name: "Conversation with Mothli" });
      await within(thread).findByText(/I wasn’t with you/);
      expect(within(thread).queryByRole("button")).toBeNull();
      expect(mocks.readChapters).not.toHaveBeenCalled();
    });
  });

  describe("offers to add a suggested book to the shelf", () => {
    const emma = { classic_id: "158", title: "Emma", author: "Jane Austen" };
    const answerWith = (suggested?: unknown[]) => ({
      insufficient_evidence: false,
      claims: [{ text: "You might like Emma by Jane Austen.", citation_ids: [] }],
      ...(suggested ? { suggested } : {}),
    });
    const suggest = async () => {
      ask("Suggest something new?");
      return screen.getByRole("region", { name: "Conversation with Mothli" });
    };

    // Catches: a suggestion the reader can do nothing with; the button under the wrong reply; the
    // wrong book added; the shelf beside the chat still not showing the book.
    test("the book a reply named can be added from under that reply, and the shelf is told", async () => {
      mocks.homeConversation.mockResolvedValue({ turns: history(1) });
      mocks.askHome.mockResolvedValueOnce(answerWith([emma]));
      let done: (value: unknown) => void = () => {};
      mocks.addClassic.mockImplementationOnce(() => new Promise((resolve) => { done = resolve; }));
      const changed = vi.fn();
      const { container } = render(<HomeChat onShelfChanged={changed} />);
      await screen.findByText("Answer 1");
      const thread = await suggest();
      const button = await within(thread).findByRole("button", { name: "Add Emma to my shelf" });
      const items = within(thread).getAllByRole("listitem");
      expect(items[items.length - 1].contains(button)).toBe(true);
      expect(items[0].querySelector("button")).toBeNull();
      expect(await axeAA(container)).toHaveNoViolations();
      fireEvent.click(button);
      expect(mocks.addClassic).toHaveBeenCalledWith("158");
      expect((button as HTMLButtonElement).disabled).toBe(true);
      fireEvent.click(button); // a second press while it is being added adds nothing twice
      expect(mocks.addClassic).toHaveBeenCalledTimes(1);
      expect(changed).not.toHaveBeenCalled();
      await act(async () => done({ book_id: "bkemma" }));
      expect(within(thread).queryByRole("button", { name: /Add Emma/ })).toBeNull();
      expect(within(thread).getByText("Emma is on your shelf now.")).toBeTruthy();
      expect(screen.getByRole("log", { name: "Mothli’s reply as it arrives" }).textContent).toContain("Emma is on your shelf now.");
      expect(changed).toHaveBeenCalledTimes(1);
    });

    // Catches: a failed download shown as success, or the button gone with nothing added.
    test("if the book cannot be added, Mothli says so and the button comes back", async () => {
      mocks.askHome.mockResolvedValueOnce(answerWith([emma]));
      mocks.addClassic.mockRejectedValueOnce(new ApiError("Couldn’t reach the book’s source.", 502));
      const changed = vi.fn();
      render(<HomeChat onShelfChanged={changed} />);
      await screen.findByLabelText("Ask Mothli about your books");
      const thread = await suggest();
      fireEvent.click(await within(thread).findByRole("button", { name: "Add Emma to my shelf" }));
      expect(await within(thread).findByText(/couldn’t add that/)).toBeTruthy();
      expect((within(thread).getByRole("button", { name: "Add Emma to my shelf" }) as HTMLButtonElement).disabled).toBe(false);
      expect(within(thread).queryByText(/on your shelf now/)).toBeNull();
      expect(changed).not.toHaveBeenCalled();
    });

    // Catches: an error shown for a book that is in fact on the shelf (added from the library meanwhile).
    test("a book that turns out to be on the shelf already counts as added", async () => {
      mocks.askHome.mockResolvedValueOnce(answerWith([emma]));
      mocks.addClassic.mockRejectedValueOnce(new ApiError("book already imported as 'bk9'", 409));
      render(<HomeChat />);
      await screen.findByLabelText("Ask Mothli about your books");
      const thread = await suggest();
      fireEvent.click(await within(thread).findByRole("button", { name: "Add Emma to my shelf" }));
      expect(await within(thread).findByText("Emma is on your shelf now.")).toBeTruthy();
    });

    // Catches: a button under a reply that suggested nothing the reader can add.
    test("a reply that named no listed book has no button, and clearing takes the button away", async () => {
      mocks.askHome.mockResolvedValueOnce(answerWith()).mockResolvedValueOnce(answerWith([emma]));
      render(<HomeChat />);
      await screen.findByLabelText("Ask Mothli about your books");
      const thread = await suggest();
      await within(thread).findByText(/You might like Emma/);
      expect(within(thread).queryByRole("button")).toBeNull();
      ask("Go on, suggest one?");
      await within(thread).findByRole("button", { name: "Add Emma to my shelf" });
      fireEvent.click(screen.getByRole("button", { name: "Clear conversation" }));
      fireEvent.click(screen.getByRole("button", { name: "Clear it" }));
      await waitFor(() => expect(screen.queryByRole("button", { name: "Add Emma to my shelf" })).toBeNull());
      expect(mocks.addClassic).not.toHaveBeenCalled();
    });
  });
});
