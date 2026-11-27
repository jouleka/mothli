/** ADR 0045 step 5 and ADR 0046, the screens: what a reader sees of the gate, in the chat inside
 * a book, the cast, "Story so far" and the home chat. A reader who has never had the plan (new)
 * and one whose plan ended each get the invitation; there are no free weeks. The plan sheet is a
 * stand-in here (its own tests are PlanSheet.test.tsx); Paddle is never reached. */
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, test, vi } from "vitest";
import { axeAA } from "../test-a11y";

const mocks = vi.hoisted(() => ({
  askBook: vi.fn(),
  askBookStream: vi.fn(),
  bookConversation: vi.fn(),
  clearConversation: vi.fn(),
  graph: vi.fn(),
  character: vi.fn(),
  visiblePeople: vi.fn(),
  notes: vi.fn(),
  homeConversation: vi.fn(),
  askHome: vi.fn(),
  clearHome: vi.fn(),
  plan: vi.fn(),
  owner: vi.fn(),
}));
vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: mocks,
}));
vi.mock("../pwa/offline", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../pwa/offline")>()),
  activeOfflineOwner: mocks.owner,
}));
// The plan sheet as its opener sees it: `onJoined` once the server says so, then `onClose`.
// Like the real one, it is put at the end of the page, outside the panel that opened it.
vi.mock("../shelf/PlanSheet", async () => {
  const { createElement: h } = await import("react");
  const { createPortal } = await import("react-dom");
  return {
    PLAN_TITLE_NEW: "Talk with Mothli",
    PlanSheet: ({ email, onClose, onJoined }: {
      email?: string | null; onClose: () => void; onJoined?: (plan: unknown) => void;
    }) => createPortal(
      h("div", { role: "dialog", "aria-modal": "true", "aria-label": "The plan" },
        h("p", null, `Checkout for ${email ?? "no one"}`),
        h("button", { type: "button", onClick: () => onJoined?.({ state: "paying", subscribed: true }) }, "Pay"),
        h("button", { type: "button", onClick: onClose }, "Close the sheet")),
      document.body),
  };
});

import { ApiError } from "../api";
import { AskTheBook } from "../reader/AskTheBook";
import { HomeChat } from "../shelf/HomeChat";
import { planOf } from "../shelf/plan-fakes";
import * as MothliPlan from "./MothliPlan";
import { INVITATIONS, KEEP_TALKING, KEPT_FOR_YOU, KEPT_SAID, keptInvitation, START_TALKING } from "./MothliPlan";
import { type MothliStanding, mothliStanding, noteMothliPlan, setMothliStanding } from "./mothliStanding";

const EMAIL = "reader@example.test";
const point = { id: "a".repeat(64), atom: 2, offset: 120, position_epoch: 0, available_chapter: 2 };
const reply = (text: string) => ({
  as_of_chapter: 2, insufficient_evidence: false, style: "prose" as const,
  claims: [{ text, citation_ids: [] }], citations: [], cost: null, read_frontier: point,
});
const turn = (role: "user" | "assistant", content: string) =>
  ({ role, content, atom: 2, reaction: false, created_at: "2020-01-01T10:00:00+00:00" });
const book = {
  bookId: "b",
  bookmark: 2,
  embedded: true,
  hosted: true,
  onNavigate: vi.fn(),
  onClose: vi.fn(),
  prepareReadContext: async () => point,
};

function stand(state: MothliStanding["state"], why: MothliStanding["why"] = null) {
  setMothliStanding({ state, why }, EMAIL);
}
/** What the real client does with the gate's answer: tell the app, then refuse. */
const planNeeded = (why: MothliStanding["why"]) => async () => {
  noteMothliPlan(why);
  throw new ApiError("Mothli needs the plan", 402, "mothli_plan");
};
const askInBook = (text: string) => {
  fireEvent.change(screen.getByLabelText("Message Mothli"), { target: { value: text } });
  fireEvent.submit(screen.getByLabelText("Message Mothli").closest("form")!);
};
const askAtHome = (text: string) => {
  fireEvent.change(screen.getByLabelText("Ask Mothli about your books"), { target: { value: text } });
  fireEvent.click(screen.getByRole("button", { name: "Ask" }));
};
/** No number, counter, countdown or "unlimited" in anything the reader can read. */
function noCounter(text: string) {
  expect(text).not.toMatch(/\d/);
  expect(text).not.toMatch(/unlimited|questions? left|count ?down|hours?\b|minutes?\b/i);
}
/** Nothing of what ADR 0046 removed. */
function noFreeWeeks(text: string) {
  expect(text).not.toMatch(/free weeks|two weeks|free start|\btrial\b/i);
}

// The two ways to be without the plan, each with its words and its button.
const WITHOUT = [
  ["new", null, INVITATIONS.new, START_TALKING],
  ["ended", "plan", INVITATIONS.plan, KEEP_TALKING],
] as const;

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  setMothliStanding(null, null);
  mocks.owner.mockReturnValue("local");
  mocks.bookConversation.mockReset().mockResolvedValue({ read_frontier: point, turns: [] });
  mocks.askBookStream.mockReset().mockResolvedValue(reply("Aldric was at the forge."));
  mocks.graph.mockReset().mockResolvedValue({
    as_of_chapter: 2,
    characters: [{ canonical_name: "Aldric", entity_id: 1, type: "character", revealed_at: 1 }],
    relationships: [],
  });
  mocks.notes.mockReset().mockResolvedValue({
    as_of_chapter: 2, cast: [],
    chapters: [{ chapter_key: "one", revealed_at: 1, title: "The forge", summary: "Aldric came home.",
      new_characters: [], events: [] }],
  });
  mocks.homeConversation.mockReset().mockResolvedValue({ turns: [] });
  mocks.askHome.mockReset().mockResolvedValue(reply("Your place is chapter two."));
  mocks.plan.mockReset().mockResolvedValue(planOf({ state: "ended", why: "plan", next_date: null }));
});

describe("the lines, word for word", () => {
  test("the invitations and their buttons, and none counts, names a number or a countdown", () => {
    expect(INVITATIONS.new).toBe(
      "Hi, I’m Mothli! I’d love to read along with you. I only know the pages you’ve read, so no spoilers, promise. Join me on the plan and you can ask me anything. Your books, your place and your highlights stay free, always.");
    expect(INVITATIONS.plan).toBe(
      "Your plan has ended, so I’m taking a little rest. Your books, your place and your highlights stay right here, free, always. Come back any time and we’ll pick up right where we left off.");
    expect(KEEP_TALKING).toBe("Keep talking with Mothli");
    expect(START_TALKING).toBe("Talk with Mothli");
    for (const line of [...Object.values(INVITATIONS), KEEP_TALKING, START_TALKING, KEPT_FOR_YOU, KEPT_SAID,
      keptInvitation(null), keptInvitation("plan"), keptInvitation(null, { title: "Emma" }),
      keptInvitation("plan", { title: "Emma" })]) {
      noCounter(line);
      noFreeWeeks(line);
      // Never words the owner does not want a reader to read.
      expect(line).not.toMatch(/\bAI\b|\bchat\b|\bmodel\b|credits?\b|unlimited/i);
      // No price and no number of replies where the reader's question waits.
      expect(line).not.toMatch(/\$|price|repl(y|ies)\b|month|year/i);
    }
  });

  test("the invitation under a kept question, word for word: the book, the reader's place, nothing past it", () => {
    expect(keptInvitation(null, { title: "Pride and Prejudice", place: "chapter 12" })).toBe(
      "Ooh, good one! I’d love to answer, using only the pages of Pride and Prejudice you’ve read so far, up to your page in chapter 12. Join me on the plan and I’ll answer right away.");
    expect(keptInvitation("plan", { title: "Pride and Prejudice", place: "chapter 12" })).toBe(
      "Ooh, I’ve missed these! I’d love to answer, using only the pages of Pride and Prejudice you’ve read so far, up to your page in chapter 12. Come back to the plan and I’ll answer right away.");
    // At home, or on a book's opening pages: no book or no place is named.
    expect(keptInvitation(null)).toBe(
      "Ooh, good one! I’d love to answer, using only the pages you’ve read so far. Join me on the plan and I’ll answer right away.");
    expect(keptInvitation(null, { title: "Emma", place: null })).toBe(
      "Ooh, good one! I’d love to answer, using only the pages of Emma you’ve read so far. Join me on the plan and I’ll answer right away.");
    expect(KEPT_FOR_YOU).toBe("I’ll keep this for you.");
  });

  // Catches (ADR 0046): anything of the free weeks left for a screen to say.
  test("nothing of the free weeks is left: no hello, no sign-up line, no note, no start call", () => {
    for (const name of ["MOTHLI_HELLO", "HOME_HELLO_NO_BOOKS", "SIGN_UP_LINE", "endingNote",
      "MothliEndingNote", "startMothli", "SEE_THE_PLAN", "GOT_IT"]) expect(name in MothliPlan, name).toBe(false);
  });
});

describe("without the plan: one invitation in place of each panel", () => {
  test.each(WITHOUT)("the chat in a book (%s)", async (state, why, words, button) => {
    stand(state, why);
    const { container } = render(<AskTheBook {...book} />);
    const line = screen.getByText(words);
    expect(screen.getByRole("button", { name: button })).toBeTruthy();
    // Nothing of the conversation: no stored turns asked for, nothing to clear. The box stays,
    // so a question can be asked and kept.
    expect(screen.getByLabelText("Message Mothli")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Conversation options" })).toBeNull();
    expect(mocks.bookConversation).not.toHaveBeenCalled();
    // The panel just opened: its words take focus, so they are read out.
    expect(document.activeElement).toBe(line);
    noCounter(container.textContent ?? "");
    noFreeWeeks(container.textContent ?? "");
    expect(await axeAA(container)).toHaveNoViolations();
  });

  test.each(WITHOUT)("the cast (%s)", async (state, why, words, button) => {
    stand(state, why);
    render(<AskTheBook {...book} initialTab="people" />);
    expect(screen.getByText(words)).toBeTruthy();
    expect(screen.getByRole("button", { name: button })).toBeTruthy();
    await act(async () => {});
    expect(mocks.graph).not.toHaveBeenCalled();
    expect(screen.queryByText("Aldric")).toBeNull();
  });

  test.each(WITHOUT)("“Story so far” (%s)", async (state, why, words) => {
    stand(state, why);
    render(<AskTheBook {...book} initialTab="story" />);
    expect(screen.getByText(words)).toBeTruthy();
    await act(async () => {});
    expect(mocks.notes).not.toHaveBeenCalled();
    expect(screen.queryByText("The forge")).toBeNull();
  });

  test.each(WITHOUT)("the home chat (%s)", async (state, why, words, button) => {
    stand(state, why);
    const { container } = render(<HomeChat />);
    expect(screen.getByText(words)).toBeTruthy();
    expect(screen.getByRole("button", { name: button })).toBeTruthy();
    // "What Mothli remembers" stays open: it is the reader's own.
    expect(screen.getByRole("button", { name: "What Mothli remembers" })).toBeTruthy();
    expect(screen.getByLabelText("Ask Mothli about your books")).toBeTruthy();
    expect(mocks.homeConversation).not.toHaveBeenCalled();
    // On the home screen at load, it takes no focus from the page.
    expect(document.activeElement).toBe(document.body);
    noCounter(container.textContent ?? "");
    noFreeWeeks(container.textContent ?? "");
    expect(await axeAA(container)).toHaveNoViolations();
  });

  test("at home with no book yet, a new reader gets the same invitation", async () => {
    stand("new");
    render(<HomeChat shelfEmpty />);
    expect(screen.getByText(INVITATIONS.new)).toBeTruthy();
    expect(screen.getByRole("button", { name: START_TALKING })).toBeTruthy();
    expect(mocks.homeConversation).not.toHaveBeenCalled();
  });

  test("the cast and the notes kept from before are not shown once the server says so", async () => {
    stand("paying");
    render(<AskTheBook {...book} initialTab="people" />);
    await screen.findByText("Aldric");
    act(() => noteMothliPlan("plan"));
    expect(screen.queryByText("Aldric")).toBeNull();
    expect(screen.getByText(INVITATIONS.plan)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Story so far" }));
    expect(screen.getByText(INVITATIONS.plan)).toBeTruthy();
    expect(mocks.notes).not.toHaveBeenCalled();
  });
});

describe("a question sent anyway", () => {
  // Catches: an old tab, or a reader whose plan ends under an open panel, meeting "I couldn’t get
  // a reply. Try again?" for a question Mothli will not answer.
  test.each(WITHOUT)("in a book (%s): the question kept with the invitation, never an error line",
    async (state, why) => {
      stand("paying");
      mocks.askBookStream.mockImplementation(planNeeded(why));
      render(<AskTheBook {...book} />);
      await waitFor(() => expect(mocks.bookConversation).toHaveBeenCalled());
      const box = screen.getByLabelText("Message Mothli") as HTMLTextAreaElement;
      box.focus();
      askInBook("Who is Aldric?");
      expect(await screen.findByText(keptInvitation(why))).toBeTruthy();
      expect(screen.getByText("Who is Aldric?")).toBeTruthy();
      expect(screen.queryByText(/couldn’t get a reply|Try again|Retry/)).toBeNull();
      // The box stays the reader's, with focus, and empty: the question is in the thread.
      expect(document.activeElement).toBe(box);
      expect(box.value).toBe("");
      expect(mothliStanding()).toEqual({ state, why });
    });

  test("mid-stream, at home: the invitation, never “That didn’t go through.”", async () => {
    stand("paying");
    mocks.askHome.mockImplementation(async (_question: string, onSentence: (text: string) => void) => {
      onSentence("Well,");
      return planNeeded("plan")();
    });
    render(<HomeChat />);
    await waitFor(() => expect(mocks.homeConversation).toHaveBeenCalled());
    screen.getByLabelText("Ask Mothli about your books").focus();
    askAtHome("Where did I leave off?");
    expect(await screen.findByText(keptInvitation("plan"))).toBeTruthy();
    expect(screen.getByText("Where did I leave off?")).toBeTruthy();
    expect(screen.queryByText(/didn’t go through|can’t check its reply/)).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(document.activeElement).toBe(screen.getByLabelText("Ask Mothli about your books"));
  });

  test("a stored conversation refused by the gate shows the invitation, not “I can’t reach…”", async () => {
    stand("paying");
    mocks.bookConversation.mockImplementation(planNeeded("plan"));
    render(<AskTheBook {...book} />);
    expect(await screen.findByText(INVITATIONS.plan)).toBeTruthy();
    expect(screen.queryByText(/can’t reach our earlier conversation/)).toBeNull();
  });
});

describe("joining from the invitation", () => {
  async function join(button: string = KEEP_TALKING) {
    fireEvent.click(screen.getByRole("button", { name: button }));
    const sheet = screen.getByRole("dialog", { name: "The plan" });
    expect(within(sheet).getByText(`Checkout for ${EMAIL}`)).toBeTruthy();
    fireEvent.click(within(sheet).getByRole("button", { name: "Pay" }));
    fireEvent.click(within(sheet).getByRole("button", { name: "Close the sheet" }));
    expect(screen.queryByRole("dialog", { name: "The plan" })).toBeNull();
    expect(mothliStanding()?.state).toBe("paying");
  }

  test("the chat in a book comes back with its stored conversation, and the question asked is answered", async () => {
    stand("paying");
    mocks.askBookStream.mockImplementationOnce(planNeeded("plan"));
    render(<AskTheBook {...book} />);
    await waitFor(() => expect(mocks.bookConversation).toHaveBeenCalledTimes(1));
    askInBook("Who is Aldric?");
    await screen.findByText(keptInvitation("plan"));
    mocks.bookConversation.mockResolvedValue({
      read_frontier: point, turns: [turn("user", "Who is Mitya?"), turn("assistant", "Fyodor’s eldest son.")] });
    await join();
    expect(await screen.findByText("Fyodor’s eldest son.")).toBeTruthy();
    expect(await screen.findByText("Aldric was at the forge.")).toBeTruthy();
    expect(mocks.bookConversation).toHaveBeenCalledTimes(2);
    expect(mocks.askBookStream).toHaveBeenCalledTimes(2);
    expect(mocks.askBookStream.mock.calls[1][1]).toBe("Who is Aldric?");
    expect(screen.queryByText(keptInvitation("plan"))).toBeNull();
    const box = screen.getByLabelText("Message Mothli") as HTMLTextAreaElement;
    expect(box.value).toBe("");
  });

  test("a new reader's first question: the invitation, then the chat once they join", async () => {
    stand("new");
    render(<AskTheBook {...book} />);
    expect(screen.getByText(INVITATIONS.new)).toBeTruthy();
    await join(START_TALKING);
    expect(await screen.findByLabelText("Message Mothli")).toBeTruthy();
    expect(mocks.bookConversation).toHaveBeenCalled();
    askInBook("Who is Aldric?");
    expect(await screen.findByText("Aldric was at the forge.")).toBeTruthy();
  });

  test("the cast loads", async () => {
    stand("ended", "plan");
    render(<AskTheBook {...book} initialTab="people" />);
    await join();
    expect(await screen.findByText("Aldric")).toBeTruthy();
    expect(mocks.graph).toHaveBeenCalled();
  });

  test("“Story so far” loads", async () => {
    stand("new");
    render(<AskTheBook {...book} initialTab="story" />);
    await join(START_TALKING);
    expect(await screen.findByText("The forge")).toBeTruthy();
    expect(mocks.notes).toHaveBeenCalled();
  });

  test("the home chat comes back with its stored conversation", async () => {
    stand("ended", "plan");
    mocks.homeConversation.mockResolvedValue({ turns: [turn("user", "What next?"), turn("assistant", "Try Emma.")] });
    render(<HomeChat />);
    await join();
    expect(await screen.findByText("Try Emma.")).toBeTruthy();
    expect(document.activeElement).toBe(screen.getByLabelText("Ask Mothli about your books"));
  });

  test("closing the sheet without joining puts focus back on the button", async () => {
    stand("ended", "plan");
    render(<AskTheBook {...book} />);
    fireEvent.click(screen.getByRole("button", { name: KEEP_TALKING }));
    fireEvent.click(screen.getByRole("button", { name: "Close the sheet" }));
    expect(document.activeElement).toBe(screen.getByRole("button", { name: KEEP_TALKING }));
    await waitFor(() => expect(mocks.plan).toHaveBeenCalled());
    expect(screen.getByText(INVITATIONS.plan)).toBeTruthy();
  });

  test.each(WITHOUT)("a reader already on the plan from elsewhere gets the panel back when the sheet closes (%s)",
    async (state, why, _words, button) => {
      stand(state, why);
      mocks.plan.mockResolvedValue(planOf({ state: "paying", why: null, plan: "monthly", subscribed: true }));
      render(<AskTheBook {...book} />);
      fireEvent.click(screen.getByRole("button", { name: button }));
      fireEvent.click(screen.getByRole("button", { name: "Close the sheet" }));
      expect(await screen.findByLabelText("Message Mothli")).toBeTruthy();
      expect(mothliStanding()?.state).toBe("paying");
    });

  // Catches: Escape in the plan sheet also closing the chat panel under it.
  test("the plan sheet over the chat owns the keyboard", () => {
    stand("ended", "plan");
    const onClose = vi.fn();
    render(<AskTheBook {...book} compact onClose={onClose} />);
    fireEvent.click(screen.getByRole("button", { name: KEEP_TALKING }));
    screen.getByRole("button", { name: "Pay" }).focus();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).not.toHaveBeenCalled();
  });
});

describe("a paying reader", () => {
  // Catches: anything of the gate, or of the removed free weeks, shown to a reader who pays.
  test.each([["paying"], ["nothing told"]] as const)("sees Mothli as before (%s)", async (state) => {
    if (state === "nothing told") setMothliStanding(null);
    else stand(state);
    render(<AskTheBook {...book} />);
    expect(await screen.findByText(/Hi! I’ve been reading along with you/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Cast" }));
    await screen.findByText("Aldric");
    fireEvent.click(screen.getByRole("button", { name: "Story so far" }));
    await screen.findByText("The forge");
    expect(screen.queryByRole("button", { name: KEEP_TALKING })).toBeNull();
    expect(screen.queryByRole("button", { name: START_TALKING })).toBeNull();
    noFreeWeeks(document.body.textContent ?? "");
    expect(mocks.plan).not.toHaveBeenCalled();
  });
});

describe("where the server told nothing", () => {
  // Catches: the invitation shown to a reader the server would serve.
  test("a failure is today's line, and a 402 that is not mothli_plan is not the invitation", async () => {
    mocks.askBookStream
      .mockRejectedValueOnce(new ApiError("the server said no (500)", 500))
      .mockRejectedValueOnce(new ApiError("Payment required", 402, "something_else"));
    render(<AskTheBook {...book} />);
    await waitFor(() => expect(mocks.bookConversation).toHaveBeenCalled());
    askInBook("Who is Aldric?");
    expect(await screen.findByText("I couldn’t get a reply. Try again?")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(mocks.askBookStream).toHaveBeenCalledTimes(2));
    expect(await screen.findByText("I couldn’t get a reply. Try again?")).toBeTruthy();
    expect(screen.queryByRole("button", { name: KEEP_TALKING })).toBeNull();
  });

  test("the home chat is as it was", async () => {
    mocks.askHome.mockRejectedValueOnce(new ApiError("the server said no (500)", 500));
    render(<HomeChat />);
    await waitFor(() => expect(mocks.homeConversation).toHaveBeenCalled());
    askAtHome("Where did I leave off?");
    expect(await screen.findByText("That didn’t go through. Your question is still here.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: KEEP_TALKING })).toBeNull();
  });
});

describe("keep their question (ADR 0046)", () => {
  const pride = { ...book, bookTitle: "Pride and Prejudice", place: "chapter 12" };
  const kept = (why: MothliStanding["why"]) =>
    keptInvitation(why, { title: "Pride and Prejudice", place: "chapter 12" });

  test.each(WITHOUT)("typed in a book (%s): kept as theirs, nothing asked, the invitation about their book",
    async (state, why, _words, button) => {
      stand(state, why);
      const { container } = render(<AskTheBook {...pride} />);
      const box = screen.getByLabelText("Message Mothli") as HTMLTextAreaElement;
      box.focus();
      askInBook("Who is Elizabeth?");
      const thread = screen.getByRole("region", { name: "Book conversation" });
      expect(within(thread).getByText("Who is Elizabeth?").className).toMatch(/lp-you/);
      expect(within(thread).getByText(kept(why))).toBeTruthy();
      expect(within(thread).getByRole("button", { name: button })).toBeTruthy();
      // Not "I'll keep this" until the reader has said not now; no pop-up opened by itself.
      expect(screen.queryByText(KEPT_FOR_YOU)).toBeNull();
      expect(screen.queryByRole("dialog", { name: "The plan" })).toBeNull();
      // Nothing was read, asked or reserved: the question never left the page.
      await act(async () => {});
      expect(mocks.askBookStream).not.toHaveBeenCalled();
      expect(mocks.bookConversation).not.toHaveBeenCalled();
      expect(box.value).toBe("");
      expect(document.activeElement).toBe(box);
      noFreeWeeks(container.textContent ?? "");
      expect(container.textContent).not.toMatch(/\$|\bAI\b|\bmodel\b|credits?\b|unlimited|repl(y|ies) left/i);
      expect(await axeAA(container)).toHaveNoViolations();
    });

  test("not now: the question waits, says so once, survives a reload, and is answered once they join", async () => {
    stand("new");
    const first = render(<AskTheBook {...pride} />);
    askInBook("Who is Elizabeth?");
    fireEvent.click(screen.getByRole("button", { name: START_TALKING }));
    fireEvent.click(screen.getByRole("button", { name: "Close the sheet" }));
    expect(screen.getByText(KEPT_FOR_YOU)).toBeTruthy();
    expect(screen.getByText("Who is Elizabeth?")).toBeTruthy();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: START_TALKING }));
    await waitFor(() => expect(mocks.plan).toHaveBeenCalled());
    // Kept on this device, not on the server.
    expect(JSON.stringify({ ...localStorage })).toContain("Who is Elizabeth?");
    expect(mocks.askBookStream).not.toHaveBeenCalled();
    first.unmount();

    // A reload: the question is still there, already waiting.
    render(<AskTheBook {...pride} />);
    expect(screen.getByText("Who is Elizabeth?")).toBeTruthy();
    expect(screen.getByText(kept(null))).toBeTruthy();
    expect(screen.getByText(KEPT_FOR_YOU)).toBeTruthy();

    // They join: sent once, exactly as asked, and the checked reply streams in.
    fireEvent.click(screen.getByRole("button", { name: START_TALKING }));
    fireEvent.click(screen.getByRole("button", { name: "Pay" }));
    fireEvent.click(screen.getByRole("button", { name: "Close the sheet" }));
    expect(await screen.findByText("Aldric was at the forge.")).toBeTruthy();
    expect(mocks.askBookStream).toHaveBeenCalledTimes(1);
    expect(mocks.askBookStream.mock.calls[0][1]).toBe("Who is Elizabeth?");
    expect(screen.getByText("Who is Elizabeth?").className).toMatch(/lp-you/);
    expect(JSON.stringify({ ...localStorage })).not.toContain("Who is Elizabeth?");
    expect(screen.queryByText(KEPT_FOR_YOU)).toBeNull();

    // Never twice: the plan ending and coming back, or the panel opening again, sends nothing more.
    act(() => noteMothliPlan(null));
    act(() => stand("paying"));
    await act(async () => {});
    expect(mocks.askBookStream).toHaveBeenCalledTimes(1);
  });

  test("removed, it is gone and never sent", async () => {
    stand("ended", "plan");
    const first = render(<AskTheBook {...pride} />);
    askInBook("Who is Elizabeth?");
    fireEvent.click(screen.getByRole("button", { name: "Remove it" }));
    expect(screen.queryByText("Who is Elizabeth?")).toBeNull();
    expect(screen.getByText(INVITATIONS.plan)).toBeTruthy();
    expect(document.activeElement).toBe(screen.getByLabelText("Message Mothli"));
    first.unmount();
    render(<AskTheBook {...pride} />);
    expect(screen.queryByText("Who is Elizabeth?")).toBeNull();
    act(() => stand("paying"));
    await waitFor(() => expect(mocks.bookConversation).toHaveBeenCalled());
    expect(mocks.askBookStream).not.toHaveBeenCalled();
  });

  test("edited: the words go back in the box, and asking again keeps the new ones", async () => {
    stand("new");
    render(<AskTheBook {...pride} />);
    askInBook("Who is Elizabeth?");
    fireEvent.click(screen.getByRole("button", { name: "Edit my question" }));
    const box = screen.getByLabelText("Message Mothli") as HTMLTextAreaElement;
    expect(box.value).toBe("Who is Elizabeth?");
    expect(document.activeElement).toBe(box);
    expect(screen.queryByText(kept(null))).toBeNull();
    askInBook("Who is Elizabeth Bennet?");
    expect(screen.getByText("Who is Elizabeth Bennet?")).toBeTruthy();
    expect(screen.queryByText("Who is Elizabeth?")).toBeNull();
    expect(mocks.askBookStream).not.toHaveBeenCalled();
  });

  test("a tapped name is kept as the natural question", async () => {
    stand("new");
    render(<AskTheBook {...pride} seed={{ id: "ask-name-1", kind: "ask", prompt: "Remind me who Elizabeth is." }} />);
    expect(await screen.findByText("Remind me who Elizabeth is.")).toBeTruthy();
    expect(screen.getByText(kept(null))).toBeTruthy();
    expect(mocks.askBookStream).not.toHaveBeenCalled();
  });

  test("joined elsewhere before opening the book: the waiting question is sent once on open", async () => {
    stand("new");
    const first = render(<AskTheBook {...pride} />);
    askInBook("Who is Elizabeth?");
    first.unmount();
    stand("paying");
    render(<AskTheBook {...pride} />);
    expect(await screen.findByText("Aldric was at the forge.")).toBeTruthy();
    expect(mocks.askBookStream).toHaveBeenCalledTimes(1);
  });

  test("where the server told nothing, the question waits", async () => {
    stand("new");
    const first = render(<AskTheBook {...pride} />);
    askInBook("Who is Elizabeth?");
    first.unmount();
    setMothliStanding(null);
    render(<AskTheBook {...pride} />);
    await waitFor(() => expect(mocks.bookConversation).toHaveBeenCalled());
    expect(mocks.askBookStream).not.toHaveBeenCalled();
    expect(JSON.stringify({ ...localStorage })).toContain("Who is Elizabeth?");
  });

  test("each book keeps its own question", async () => {
    stand("new");
    const first = render(<AskTheBook {...pride} />);
    askInBook("Who is Elizabeth?");
    first.unmount();
    render(<AskTheBook {...pride} bookId="other" />);
    expect(screen.queryByText("Who is Elizabeth?")).toBeNull();
  });

  test("at home: kept, survives a reload, then answered once into the conversation they had", async () => {
    stand("ended", "plan");
    const first = render(<HomeChat />);
    askAtHome("What should I read tonight?");
    expect(screen.getByText("What should I read tonight?")).toBeTruthy();
    expect(screen.getByText(keptInvitation("plan"))).toBeTruthy();
    expect(mocks.askHome).not.toHaveBeenCalled();
    expect((screen.getByLabelText("Ask Mothli about your books") as HTMLTextAreaElement).value).toBe("");
    first.unmount();

    const { container } = render(<HomeChat />);
    expect(screen.getByText("What should I read tonight?")).toBeTruthy();
    expect(screen.getByText(KEPT_FOR_YOU)).toBeTruthy();
    noFreeWeeks(container.textContent ?? "");
    expect(await axeAA(container)).toHaveNoViolations();

    mocks.homeConversation.mockResolvedValue({ turns: [turn("user", "What next?"), turn("assistant", "Try Emma.")] });
    await join();
    const talk = await screen.findByRole("region", { name: "Conversation with Mothli" });
    expect(await within(talk).findByText("Your place is chapter two.")).toBeTruthy();
    expect(screen.getByText("Try Emma.")).toBeTruthy();
    expect(mocks.askHome).toHaveBeenCalledTimes(1);
    expect(mocks.askHome.mock.calls[0][0]).toBe("What should I read tonight?");
    expect(JSON.stringify({ ...localStorage })).not.toContain("What should I read tonight?");
  });

  async function join() {
    fireEvent.click(screen.getByRole("button", { name: KEEP_TALKING }));
    fireEvent.click(screen.getByRole("button", { name: "Pay" }));
    fireEvent.click(screen.getByRole("button", { name: "Close the sheet" }));
  }

  test("a paying reader's question is asked, never kept", async () => {
    stand("paying");
    render(<AskTheBook {...pride} />);
    await waitFor(() => expect(mocks.bookConversation).toHaveBeenCalled());
    askInBook("Who is Elizabeth?");
    expect(await screen.findByText("Aldric was at the forge.")).toBeTruthy();
    expect(screen.queryByText(kept(null))).toBeNull();
    expect(JSON.stringify({ ...localStorage })).not.toContain("Who is Elizabeth?");
  });
});
