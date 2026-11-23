/** ADR 0045 step 5, the screens: what a reader sees of the two free weeks and of the gate, in the
 * chat inside a book, the cast, "Story so far" and the home chat. The plan sheet is a stand-in
 * here (its own tests are PlanSheet.test.tsx); Paddle is never reached. */
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
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
  startMothli: vi.fn(),
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
import {
  endingNote,
  endingWeekday,
  endingWhen,
  forgetEndingDay,
  GOT_IT,
  HOME_HELLO_NO_BOOKS,
  INVITATIONS,
  KEEP_TALKING,
  MOTHLI_HELLO,
  SEE_THE_PLAN,
  SIGN_UP_LINE,
} from "./MothliPlan";
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

function stand(state: MothliStanding["state"], why: MothliStanding["why"] = null, ending_note = false) {
  setMothliStanding({ state, why, ending_note }, EMAIL);
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

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  forgetEndingDay();
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
  mocks.plan.mockReset().mockResolvedValue(planOf({ state: "ended", why: "time", next_date: null }));
  mocks.startMothli.mockReset().mockResolvedValue({ state: "free", why: null, ending_note: false });
});

describe("the lines, word for word", () => {
  test("section 6's lines, and none counts, names a number or a countdown", () => {
    expect(SIGN_UP_LINE).toBe("Reading here is free, always. Talking with me is free for your first two weeks.");
    expect(MOTHLI_HELLO).toBe(
      "Hi, I’m Mothli! Your first two weeks with me are free, so ask me anything about the pages you’ve read. No spoilers, promise.");
    expect(endingNote("today")).toBe(
      "Just so you know, our free weeks together end today. Your books stay yours either way. If you’d like to keep talking after that, here’s how.");
    expect(endingNote("tomorrow")).toBe(
      "Just so you know, our free weeks together end tomorrow. Your books stay yours either way. If you’d like to keep talking after that, here’s how.");
    expect(endingNote("on Thursday")).toBe(
      "Just so you know, our free weeks together end on Thursday. Your books stay yours either way. If you’d like to keep talking after that, here’s how.");
    expect(INVITATIONS.time).toBe(
      "Our two free weeks are over. I loved reading along with you! Your books, your place and your highlights stay right here, free, always. Join me on the plan and we’ll pick up right where we left off.");
    expect(INVITATIONS.ceiling).toBe(
      "We’ve talked so much these first days that your free start with me is all used up. That’s a good sign! Your books, your place and your highlights stay right here, free, always. Join me on the plan and we can keep going right now.");
    expect(INVITATIONS.plan).toBe(
      "Your plan has ended, so I’m taking a little rest. Your books, your place and your highlights stay right here, free, always. Come back any time and we’ll pick up right where we left off.");
    expect(KEEP_TALKING).toBe("Keep talking with Mothli");
    expect(SEE_THE_PLAN).toBe("See the plan");
    expect(GOT_IT).toBe("Got it");
    for (const line of [SIGN_UP_LINE, MOTHLI_HELLO, endingNote("today"), endingNote("tomorrow"),
      endingNote("on Thursday"), ...Object.values(INVITATIONS), KEEP_TALKING, SEE_THE_PLAN, GOT_IT]) noCounter(line);
  });

  // Catches: the day said by the server's UTC calendar rather than the reader's own, or a bare
  // weekday ("on Thursday" said on a Thursday) in the last two days, where it is ambiguous.
  test("today, tomorrow or the weekday, by the reader's own calendar day, not UTC's", () => {
    // Auckland is UTC+13 in December. The clock: 1 December 20:00 UTC, 2 December 09:00 there.
    const now = new Date("2026-12-01T20:00:00Z");
    const auckland = "Pacific/Auckland";
    // 2 December 10:00 UTC is tomorrow in UTC but 23:00 today in Auckland.
    expect(endingWhen("2026-12-02T10:00:00Z", now, auckland)).toBe("today");
    expect(endingWhen("2026-12-02T10:00:00Z", now, "UTC")).toBe("tomorrow");
    // 2 December 12:00 UTC is 3 December 01:00 in Auckland: tomorrow there.
    expect(endingWhen("2026-12-02T12:00:00Z", now, auckland)).toBe("tomorrow");
    // 3 December 12:00 UTC is Friday 4 December in Auckland, though Thursday in UTC.
    expect(endingWhen("2026-12-03T12:00:00Z", now, auckland)).toBe("on Friday");
    expect(endingWhen("2026-12-03T12:00:00Z", now, "UTC")).toBe("on Thursday");
    // Los Angeles is UTC−8: at 2 December 03:00 UTC it is still 1 December there, so the end at
    // 2 December 20:00 UTC is today in UTC and tomorrow in Los Angeles.
    const evening = new Date("2026-12-02T03:00:00Z");
    expect(endingWhen("2026-12-02T20:00:00Z", evening, "UTC")).toBe("today");
    expect(endingWhen("2026-12-02T20:00:00Z", evening, "America/Los_Angeles")).toBe("tomorrow");
    expect(endingWhen("not a date", now)).toBeNull();
  });

  test("the day the free weeks end is the weekday in the reader's own time zone", () => {
    expect(endingWeekday("2026-12-03T12:00:00Z", "UTC")).toBe("Thursday");
    // The same moment is still Thursday in Los Angeles and already Friday in Kiritimati.
    expect(endingWeekday("2026-12-03T23:30:00Z", "America/Los_Angeles")).toBe("Thursday");
    expect(endingWeekday("2026-12-03T23:30:00Z", "Pacific/Kiritimati")).toBe("Friday");
    expect(endingWeekday("not a date")).toBeNull();
  });
});

describe("ended: one invitation in place of each panel", () => {
  const whys = ["time", "ceiling", "plan"] as const;

  test.each(whys)("the chat in a book (%s)", async (why) => {
    stand("ended", why);
    const { container } = render(<AskTheBook {...book} />);
    const line = screen.getByText(INVITATIONS[why]);
    expect(screen.getByRole("button", { name: KEEP_TALKING })).toBeTruthy();
    // Nothing of the conversation: no box, no stored turns asked for, nothing to clear.
    expect(screen.queryByLabelText("Message Mothli")).toBeNull();
    expect(screen.queryByRole("button", { name: "Conversation options" })).toBeNull();
    expect(mocks.bookConversation).not.toHaveBeenCalled();
    // The panel just opened: its words take focus, so they are read out.
    expect(document.activeElement).toBe(line);
    noCounter(container.textContent ?? "");
    expect(await axeAA(container)).toHaveNoViolations();
  });

  test.each(whys)("the cast (%s)", async (why) => {
    stand("ended", why);
    render(<AskTheBook {...book} initialTab="people" />);
    expect(screen.getByText(INVITATIONS[why])).toBeTruthy();
    expect(screen.getByRole("button", { name: KEEP_TALKING })).toBeTruthy();
    await act(async () => {});
    expect(mocks.graph).not.toHaveBeenCalled();
    expect(mocks.startMothli).not.toHaveBeenCalled();
    expect(screen.queryByText("Aldric")).toBeNull();
  });

  test.each(whys)("“Story so far” (%s)", async (why) => {
    stand("ended", why);
    render(<AskTheBook {...book} initialTab="story" />);
    expect(screen.getByText(INVITATIONS[why])).toBeTruthy();
    await act(async () => {});
    expect(mocks.notes).not.toHaveBeenCalled();
    expect(screen.queryByText("The forge")).toBeNull();
  });

  test.each(whys)("the home chat (%s)", async (why) => {
    stand("ended", why);
    const { container } = render(<HomeChat />);
    expect(screen.getByText(INVITATIONS[why])).toBeTruthy();
    expect(screen.getByRole("button", { name: KEEP_TALKING })).toBeTruthy();
    // "What Mothli remembers" stays open: it is the reader's own.
    expect(screen.getByRole("button", { name: "What Mothli remembers" })).toBeTruthy();
    expect(screen.queryByLabelText("Ask Mothli about your books")).toBeNull();
    expect(mocks.homeConversation).not.toHaveBeenCalled();
    // On the home screen at load, it takes no focus from the page.
    expect(document.activeElement).toBe(document.body);
    noCounter(container.textContent ?? "");
    expect(await axeAA(container)).toHaveNoViolations();
  });

  test("the cast and the notes kept from before are not shown once the server says so", async () => {
    stand("free");
    render(<AskTheBook {...book} initialTab="people" />);
    await screen.findByText("Aldric");
    act(() => noteMothliPlan("time"));
    expect(screen.queryByText("Aldric")).toBeNull();
    expect(screen.getByText(INVITATIONS.time)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Story so far" }));
    expect(screen.getByText(INVITATIONS.time)).toBeTruthy();
    expect(mocks.notes).not.toHaveBeenCalled();
  });
});

describe("a question sent anyway", () => {
  // Catches: an old tab, or a reader whose free weeks end under an open panel, meeting "I couldn’t
  // get a reply. Try again?" for a question Mothli will not answer.
  test("in a book: the invitation where the reply would be, never an error line", async () => {
    stand("free");
    mocks.askBookStream.mockImplementation(planNeeded("ceiling"));
    render(<AskTheBook {...book} />);
    await waitFor(() => expect(mocks.bookConversation).toHaveBeenCalled());
    screen.getByLabelText("Message Mothli").focus();
    askInBook("Who is Aldric?");
    const line = await screen.findByText(INVITATIONS.ceiling);
    expect(screen.queryByText(/couldn’t get a reply|Try again|Retry/)).toBeNull();
    // The box went with the panel's content: focus moves to the invitation, so it is read out.
    expect(document.activeElement).toBe(line);
    expect(mothliStanding()).toEqual({ state: "ended", why: "ceiling", ending_note: false });
  });

  test("mid-stream, at home: the invitation, never “That didn’t go through.”", async () => {
    stand("free");
    mocks.askHome.mockImplementation(async (_question: string, onSentence: (text: string) => void) => {
      onSentence("Well,");
      return planNeeded("time")();
    });
    render(<HomeChat />);
    await waitFor(() => expect(mocks.homeConversation).toHaveBeenCalled());
    screen.getByLabelText("Ask Mothli about your books").focus();
    askAtHome("Where did I leave off?");
    const line = await screen.findByText(INVITATIONS.time);
    expect(screen.queryByText(/didn’t go through|can’t check its reply/)).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(document.activeElement).toBe(line);
  });

  test("a stored conversation refused by the gate shows the invitation, not “I can’t reach…”", async () => {
    stand("free");
    mocks.bookConversation.mockImplementation(planNeeded("plan"));
    render(<AskTheBook {...book} />);
    expect(await screen.findByText(INVITATIONS.plan)).toBeTruthy();
    expect(screen.queryByText(/can’t reach our earlier conversation/)).toBeNull();
  });
});

describe("joining from the invitation", () => {
  async function join() {
    fireEvent.click(screen.getByRole("button", { name: KEEP_TALKING }));
    const sheet = screen.getByRole("dialog", { name: "The plan" });
    expect(within(sheet).getByText(`Checkout for ${EMAIL}`)).toBeTruthy();
    fireEvent.click(within(sheet).getByRole("button", { name: "Pay" }));
    fireEvent.click(within(sheet).getByRole("button", { name: "Close the sheet" }));
    expect(screen.queryByRole("dialog", { name: "The plan" })).toBeNull();
    expect(mothliStanding()?.state).toBe("paying");
  }

  test("the chat in a book comes back with its stored conversation and the question asked", async () => {
    stand("free");
    mocks.askBookStream.mockImplementationOnce(planNeeded("time"));
    render(<AskTheBook {...book} />);
    await waitFor(() => expect(mocks.bookConversation).toHaveBeenCalledTimes(1));
    askInBook("Who is Aldric?");
    await screen.findByText(INVITATIONS.time);
    mocks.bookConversation.mockResolvedValue({
      read_frontier: point, turns: [turn("user", "Who is Mitya?"), turn("assistant", "Fyodor’s eldest son.")] });
    await join();
    expect(await screen.findByText("Fyodor’s eldest son.")).toBeTruthy();
    expect(mocks.bookConversation).toHaveBeenCalledTimes(2);
    expect(screen.queryByText(INVITATIONS.time)).toBeNull();
    // The question is still in the box, and the box has focus.
    const box = screen.getByLabelText("Message Mothli") as HTMLTextAreaElement;
    expect(box.value).toBe("Who is Aldric?");
    expect(document.activeElement).toBe(box);
  });

  test("the cast loads", async () => {
    stand("ended", "time");
    render(<AskTheBook {...book} initialTab="people" />);
    await join();
    expect(await screen.findByText("Aldric")).toBeTruthy();
    expect(mocks.graph).toHaveBeenCalled();
  });

  test("“Story so far” loads", async () => {
    stand("ended", "ceiling");
    render(<AskTheBook {...book} initialTab="story" />);
    await join();
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
    stand("ended", "time");
    render(<AskTheBook {...book} />);
    fireEvent.click(screen.getByRole("button", { name: KEEP_TALKING }));
    fireEvent.click(screen.getByRole("button", { name: "Close the sheet" }));
    expect(document.activeElement).toBe(screen.getByRole("button", { name: KEEP_TALKING }));
    await waitFor(() => expect(mocks.plan).toHaveBeenCalled());
    expect(screen.getByText(INVITATIONS.time)).toBeTruthy();
  });

  test("a reader already on the plan from elsewhere gets the panel back when the sheet closes", async () => {
    stand("ended", "time");
    mocks.plan.mockResolvedValue(planOf({ state: "paying", plan: "monthly", subscribed: true }));
    render(<AskTheBook {...book} />);
    fireEvent.click(screen.getByRole("button", { name: KEEP_TALKING }));
    fireEvent.click(screen.getByRole("button", { name: "Close the sheet" }));
    expect(await screen.findByLabelText("Message Mothli")).toBeTruthy();
    expect(mothliStanding()?.state).toBe("paying");
  });

  // Catches: Escape in the plan sheet also closing the chat panel under it.
  test("the plan sheet over the chat owns the keyboard", () => {
    stand("ended", "time");
    const onClose = vi.fn();
    render(<AskTheBook {...book} compact onClose={onClose} />);
    fireEvent.click(screen.getByRole("button", { name: KEEP_TALKING }));
    screen.getByRole("button", { name: "Pay" }).focus();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).not.toHaveBeenCalled();
  });
});

describe("not started: the start call and the hello", () => {
  test("opening the cast or “Story so far” by the reader's own tap starts the free weeks, once", async () => {
    stand("new");
    render(<AskTheBook {...book} />);
    await act(async () => {});
    expect(mocks.startMothli).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Cast" }));
    await waitFor(() => expect(mothliStanding()?.state).toBe("free"));
    expect(mocks.startMothli).toHaveBeenCalledTimes(1);
    expect(mocks.startMothli).toHaveBeenCalledWith("b");
    expect(await screen.findByText("Aldric")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Story so far" }));
    await screen.findByText("The forge");
    expect(mocks.startMothli).toHaveBeenCalledTimes(1);
  });

  test("“Story so far” opened from elsewhere by a tap, and “Who was that?”, start it too", async () => {
    stand("new");
    const first = render(<AskTheBook {...book} initialTab="story" />);
    await waitFor(() => expect(mocks.startMothli).toHaveBeenCalledWith("b"));
    first.unmount();
    stand("new");
    mocks.startMothli.mockClear();
    render(<AskTheBook {...book} />);
    fireEvent.click(screen.getByRole("button", { name: "Who was that?" }));
    await waitFor(() => expect(mocks.startMothli).toHaveBeenCalledWith("b"));
  });

  test.each([["free"], ["paying"], ["nothing told"]] as const)(
    "a reader %s is never sent to the start route", async (state) => {
      if (state === "nothing told") setMothliStanding(null);
      else stand(state);
      render(<AskTheBook {...book} />);
      fireEvent.click(screen.getByRole("button", { name: "Cast" }));
      await screen.findByText("Aldric");
      fireEvent.click(screen.getByRole("button", { name: "Story so far" }));
      await screen.findByText("The forge");
      expect(mocks.startMothli).not.toHaveBeenCalled();
    });

  test("the hello in a book, above the box, until the first reply arrives", async () => {
    stand("new");
    render(<AskTheBook {...book} />);
    expect(await screen.findByText(MOTHLI_HELLO)).toBeTruthy();
    let finish!: (value: unknown) => void;
    mocks.askBookStream.mockImplementationOnce(() => new Promise((done) => { finish = done; }));
    askInBook("Who is Aldric?");
    await waitFor(() => expect(mocks.askBookStream).toHaveBeenCalled());
    // Still there while the first reply is on its way.
    expect(screen.getByText(MOTHLI_HELLO)).toBeTruthy();
    await act(async () => finish(reply("Aldric was at the forge.")));
    await screen.findByText("Aldric was at the forge.");
    expect(screen.queryByText(MOTHLI_HELLO)).toBeNull();
    expect(mothliStanding()?.state).toBe("free");
  });

  test("the hello at home, until the first reply arrives", async () => {
    stand("new");
    render(<HomeChat />);
    expect(await screen.findByText(MOTHLI_HELLO)).toBeTruthy();
    askAtHome("Where did I leave off?");
    await screen.findAllByText("Your place is chapter two.");
    expect(screen.queryByText(MOTHLI_HELLO)).toBeNull();
  });

  // Catches: a second "Hi, I’m Mothli" under the empty shelf's own, or a hello inviting
  // questions "about the pages you’ve read" to a reader with no book yet (L5 of the plan walk).
  test("at home with no book yet: one hello, and a line that suits an empty shelf", async () => {
    stand("new");
    render(<HomeChat shelfEmpty />);
    const hello = await screen.findByText(HOME_HELLO_NO_BOOKS);
    expect(HOME_HELLO_NO_BOOKS).toBe("Your first two weeks with me are free, starting the first time you try me out.");
    expect(screen.queryByText(MOTHLI_HELLO)).toBeNull();
    expect(document.body.textContent).not.toMatch(/Hi, I’m Mothli/);
    expect(hello.textContent).not.toMatch(/pages you’ve read/);
    noCounter(HOME_HELLO_NO_BOOKS);
  });

  test.each([["free"], ["paying"], ["nothing told"]] as const)("no hello for a reader %s", async (state) => {
    if (state === "nothing told") setMothliStanding(null);
    else stand(state);
    render(<AskTheBook {...book} />);
    expect(await screen.findByText(/Hi! I’ve been reading along with you/)).toBeTruthy();
    expect(screen.queryByText(MOTHLI_HELLO)).toBeNull();
  });
});

describe("the gentle note in the last two days", () => {
  // A fixed clock exactly a day before the end: "tomorrow" in every time zone.
  const ends = "2026-12-03T12:00:00Z";
  const day = () => "tomorrow";
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-12-02T12:00:00Z"));
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  test("at the top of the chat, saying when; “Got it” hides it on this device", async () => {
    stand("free", null, true);
    mocks.plan.mockResolvedValue(planOf({ state: "free", next_date: ends }));
    const first = render(<AskTheBook {...book} />);
    const note = await screen.findByText(endingNote(day()));
    noCounter(note.textContent!.replace(day(), ""));
    expect(screen.getByRole("button", { name: SEE_THE_PLAN })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: GOT_IT }));
    expect(screen.queryByText(endingNote(day()))).toBeNull();
    expect(document.activeElement).toBe(screen.getByLabelText("Message Mothli"));
    first.unmount();
    // Remembered on this device: not again, in a book or at home.
    render(<AskTheBook {...book} />);
    render(<HomeChat />);
    await waitFor(() => expect(mocks.homeConversation).toHaveBeenCalled());
    await act(async () => {});
    expect(screen.queryByText(endingNote(day()))).toBeNull();
  });

  // Catches: the book chat opening scrolled to the latest exchange, so on a phone the note's
  // first line is cut off at the top (M6 of the plan walk, 23 November 2026). jsdom has no
  // layout: the latest exchange is placed 400 pixels down, the note at the top.
  test("with an earlier conversation, the note is shown whole, not scrolled past", async () => {
    const tops = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetTop");
    const scroll = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "scrollTop");
    const scrolled = new WeakMap<Element, number>();
    Object.defineProperty(HTMLElement.prototype, "offsetTop", {
      configurable: true, get(this: HTMLElement) { return this.hasAttribute("data-latest-exchange") ? 400 : 0; },
    });
    Object.defineProperty(HTMLElement.prototype, "scrollTop", {
      configurable: true,
      get(this: HTMLElement) { return scrolled.get(this) ?? 0; },
      set(this: HTMLElement, value: number) { scrolled.set(this, value); },
    });
    try {
      mocks.bookConversation.mockResolvedValue({ read_frontier: point,
        turns: [turn("user", "Who is Aldric?"), turn("assistant", "Aldric keeps the forge.")] });
      // Without the note: the thread opens at the latest exchange, as before.
      stand("free", null, false);
      const plain = render(<AskTheBook {...book} />);
      await screen.findByText("Aldric keeps the forge.");
      const thread = () => screen.getByRole("region", { name: "Book conversation" });
      await waitFor(() => expect(thread().scrollTop).toBe(400));
      plain.unmount();
      // With it: the note, whole, at the top.
      stand("free", null, true);
      mocks.plan.mockResolvedValue(planOf({ state: "free", next_date: ends }));
      render(<AskTheBook {...book} />);
      await screen.findByText("Aldric keeps the forge.");
      await screen.findByText(endingNote(day()));
      await waitFor(() => expect(thread().scrollTop).toBe(0));
    } finally {
      if (tops) Object.defineProperty(HTMLElement.prototype, "offsetTop", tops);
      else delete (HTMLElement.prototype as { offsetTop?: number }).offsetTop;
      if (scroll) Object.defineProperty(HTMLElement.prototype, "scrollTop", scroll);
      else delete (HTMLElement.prototype as { scrollTop?: number }).scrollTop;
    }
  });

  // Catches: "Got it" kept for the device, so a second reader signed in here later never sees
  // their own note (L3 of the plan walk, 23 November 2026).
  test("“Got it” is remembered for each reader on the device, not for the device", async () => {
    const note = { state: "free", why: null, ending_note: true };
    mocks.plan.mockResolvedValue(planOf({ state: "free", next_date: ends }));
    setMothliStanding(note, EMAIL, "reader-a");
    const first = render(<AskTheBook {...book} />);
    await screen.findByText(endingNote(day()));
    fireEvent.click(screen.getByRole("button", { name: GOT_IT }));
    first.unmount();
    expect(localStorage.getItem("mothli.ending-note-seen:reader-a")).toBe("yes");
    expect(localStorage.getItem("mothli.ending-note-seen")).toBeNull();
    // Another reader signs in on the same device: their own note.
    setMothliStanding(note, "other@example.test", "reader-b");
    const second = render(<AskTheBook {...book} />);
    expect(await screen.findByText(endingNote(day()))).toBeTruthy();
    second.unmount();
    // The first reader again: still hidden for them.
    setMothliStanding(note, EMAIL, "reader-a");
    render(<AskTheBook {...book} />);
    await waitFor(() => expect(mocks.bookConversation).toHaveBeenCalled());
    await act(async () => {});
    expect(screen.queryByText(endingNote(day()))).toBeNull();
    setMothliStanding(null, null, null);
  });

  test("at home too, and “See the plan” opens the plan sheet and comes back to its button", async () => {
    stand("free", null, true);
    mocks.plan.mockResolvedValue(planOf({ state: "free", next_date: ends }));
    const { container } = render(<HomeChat />);
    expect(await screen.findByText(endingNote(day()))).toBeTruthy();
    expect(await axeAA(container)).toHaveNoViolations();
    fireEvent.click(screen.getByRole("button", { name: SEE_THE_PLAN }));
    fireEvent.click(screen.getByRole("button", { name: "Close the sheet" }));
    expect(document.activeElement).toBe(screen.getByRole("button", { name: SEE_THE_PLAN }));
    // Joining from it: the note goes, the chat stays, and the box has focus.
    fireEvent.click(screen.getByRole("button", { name: SEE_THE_PLAN }));
    fireEvent.click(screen.getByRole("button", { name: "Pay" }));
    fireEvent.click(screen.getByRole("button", { name: "Close the sheet" }));
    expect(screen.queryByText(endingNote(day()))).toBeNull();
    expect(mothliStanding()?.state).toBe("paying");
    expect(document.activeElement).toBe(screen.getByLabelText("Ask Mothli about your books"));
  });

  test.each([
    ["free, not yet the last two days", { state: "free", why: null, ending_note: false }],
    ["new", { state: "new", why: null, ending_note: false }],
    ["paying", { state: "paying", why: null, ending_note: false }],
    ["nothing told", null],
  ] as const)("no note and no plan asked for when %s", async (_name, standing) => {
    setMothliStanding(standing);
    render(<AskTheBook {...book} />);
    render(<HomeChat />);
    await waitFor(() => expect(mocks.homeConversation).toHaveBeenCalled());
    await act(async () => {});
    expect(screen.queryByText(/our free weeks together end/)).toBeNull();
    expect(mocks.plan).not.toHaveBeenCalled();
  });

  test("without the day, the note is not said", async () => {
    stand("free", null, true);
    mocks.plan.mockRejectedValue(new ApiError("unavailable", 503));
    render(<AskTheBook {...book} />);
    await waitFor(() => expect(mocks.plan).toHaveBeenCalled());
    await act(async () => {});
    expect(screen.queryByText(/our free weeks together end/)).toBeNull();
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
    expect(screen.queryByText(MOTHLI_HELLO)).toBeNull();
    askAtHome("Where did I leave off?");
    expect(await screen.findByText("That didn’t go through. Your question is still here.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: KEEP_TALKING })).toBeNull();
  });
});
