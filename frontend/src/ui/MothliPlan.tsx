/** Hosted mode: what a reader sees of the two free weeks and of the gate (ADR 0045 section 6).
 *
 * - The hello, for a reader who has never used Mothli (shown by the chats).
 * - The gentle note, once, in the free weeks' last two days, naming the day they end in the
 *   reader's own time zone, with "See the plan" and "Got it" (remembered on this device).
 * - The invitation, for a reader whose free weeks or plan are over, in place of a panel's
 *   content (the chat in a book, the home chat, the cast, "Story so far"), worded by why, with
 *   one button that opens the plan sheet. Joining updates where the reader stands at once, and
 *   each panel comes back without a reload.
 *
 * No number, no counter, no countdown, never "unlimited". The lines are section 6's, word for word. */
import { type RefObject, useEffect, useRef, useState } from "react";
import { api } from "../api";
import { PlanSheet } from "../shelf/PlanSheet";
import { Button } from "./Button";
import { Moth } from "./Moth";
import {
  dismissEndingNote,
  endingNoteSeen,
  mothliEmail,
  mothliStanding,
  type MothliStanding,
  noteMothliJoined,
  setMothliStanding,
  useMothliStanding,
} from "./mothliStanding";
import "./mothli-plan.css";

export const MOTHLI_HELLO =
  "Hi, I’m Mothli! Your first two weeks with me are free, so ask me anything about the pages you’ve read. No spoilers, promise.";
export const SIGN_UP_LINE = "Reading here is free, always. Talking with me is free for your first two weeks.";
export const INVITATIONS: Record<"time" | "ceiling" | "plan", string> = {
  time: "Our two free weeks are over. I loved reading along with you! Your books, your place and your highlights stay right here, free, always. Join me on the plan and we’ll pick up right where we left off.",
  ceiling: "We’ve talked so much these first days that your free start with me is all used up. That’s a good sign! Your books, your place and your highlights stay right here, free, always. Join me on the plan and we can keep going right now.",
  plan: "Your plan has ended, so I’m taking a little rest. Your books, your place and your highlights stay right here, free, always. Come back any time and we’ll pick up right where we left off.",
};
export const KEEP_TALKING = "Keep talking with Mothli";
export const SEE_THE_PLAN = "See the plan";
export const GOT_IT = "Got it";
export const endingNote = (day: string) =>
  `Just so you know, our free weeks together end on ${day}. Your books stay yours either way. If you’d like to keep talking after that, here’s how.`;

/** The weekday a moment falls on in the reader's own time zone ("Thursday"); no hours. */
export function endingWeekday(iso: string, timeZone?: string): string | null {
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) return null;
  return when.toLocaleDateString("en-GB", { weekday: "long", timeZone });
}

// The day the free weeks end: asked of the plan once a page, only when the note is due.
let dayAsked: Promise<string | null> | null = null;
function endingDay(): Promise<string | null> {
  dayAsked ??= api.plan()
    .then((plan) => (plan.state === "free" && plan.next_date ? endingWeekday(plan.next_date) : null))
    .catch(() => {
      dayAsked = null;
      return null;
    });
  return dayAsked;
}

/** Tests only: ask for the day again. */
export function forgetEndingDay(): void {
  dayAsked = null;
}

// The start call: once, from the reader's own tap on the cast or "Story so far".
let starting = false;
/** The reader opened the cast or "Story so far" themselves. For a reader who has never used
 * Mothli this starts their free weeks (the server's start route, once); for anyone else, and
 * where the server told nothing, it does nothing. Never called from the app's own fetches. */
export function startMothli(bookId?: string): void {
  if (mothliStanding()?.state !== "new" || starting || typeof api.startMothli !== "function") return;
  starting = true;
  api.startMothli(bookId)
    .then((answer) => setMothliStanding(answer))
    .catch(() => { /* Asked again at the next tap; the panel works as before meanwhile. */ })
    .finally(() => { starting = false; });
}

/** In place of a panel's content for an ended reader. The text takes focus, so a screen reader
 * reads it, when it replaces content that held focus (`replaced`: the panel showed its content
 * before) or when focus is outside `scope` (a dialog panel that just opened); it never takes
 * focus from elsewhere on the page, nor on a page just loaded. */
export function MothliInvitation({
  why,
  home = false,
  scope,
  replaced = true,
}: {
  why: MothliStanding["why"];
  /** The home chat's handwriting rather than a panel's. */
  home?: boolean;
  scope?: RefObject<HTMLElement>;
  replaced?: boolean;
}) {
  const [seeing, setSeeing] = useState(false);
  const joined = useRef(false);
  const backToButton = useRef(false);
  const textRef = useRef<HTMLParagraphElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    const active = document.activeElement;
    const lost = !active || active === document.body;
    if ((replaced && lost) || (scope?.current && !scope.current.contains(active)))
      textRef.current?.focus({ preventScroll: true });
  }, []);

  useEffect(() => {
    if (seeing) return;
    if (joined.current) {
      // The panel comes back in its place; it puts focus where the reader can carry on.
      noteMothliJoined();
    } else if (backToButton.current) {
      backToButton.current = false;
      buttonRef.current?.focus();
    }
  }, [seeing]);

  const close = () => {
    if (!joined.current) {
      backToButton.current = true;
      // Already on the plan from elsewhere (another tab, another device)? Then the panel comes back.
      void api.plan()
        .then((plan) => {
          if (plan.state !== "ended" && mothliStanding()?.state === "ended")
            setMothliStanding({ state: plan.state, why: plan.why, ending_note: false });
        })
        .catch(() => { /* Nothing changes. */ });
    }
    setSeeing(false);
  };

  return (
    <div className={home ? "ml-hc-hello mothli-invite mothli-invite--home" : "memory-state mothli-invite"}>
      {!home && <Moth mood={why === "plan" ? "sleepy" : "curious"} size={64} />}
      <p ref={textRef} tabIndex={-1} className={home ? "ml-hc-hand" : "lp-voice"}>
        {INVITATIONS[why ?? "time"]}
      </p>
      <div>
        <Button ref={buttonRef} variant="primary" aria-haspopup="dialog" onClick={() => setSeeing(true)}>
          {KEEP_TALKING}
        </Button>
      </div>
      {seeing && (
        <PlanSheet
          email={mothliEmail()}
          onJoined={() => {
            joined.current = true;
          }}
          onClose={close}
        />
      )}
    </div>
  );
}

/** The gentle note at the top of a chat panel, in the free weeks' last two days, until "Got it".
 * `after` is where focus goes once the note is gone (the chat's own box). */
export function MothliEndingNote({ home = false, after }: { home?: boolean; after?: RefObject<HTMLElement> }) {
  const standing = useMothliStanding();
  const due = standing?.state === "free" && standing.ending_note && !endingNoteSeen();
  const [day, setDay] = useState<string | null>(null);
  const [seeing, setSeeing] = useState(false);
  const joined = useRef(false);
  const backToButton = useRef(false);
  const seeRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!due) return;
    let live = true;
    void endingDay().then((found) => {
      if (live) setDay(found);
    });
    return () => {
      live = false;
    };
  }, [due]);

  useEffect(() => {
    if (seeing) return;
    if (joined.current) {
      joined.current = false;
      after?.current?.focus();
      noteMothliJoined();
    } else if (backToButton.current) {
      backToButton.current = false;
      seeRef.current?.focus();
    }
  }, [seeing]);

  // Without the day the note cannot be said as written, so it is not said.
  if (!due || !day) return null;
  const gotIt = () => {
    after?.current?.focus();
    dismissEndingNote();
  };
  const sheet = seeing && (
    <PlanSheet
      email={mothliEmail()}
      onJoined={() => {
        joined.current = true;
      }}
      onClose={() => {
        if (!joined.current) backToButton.current = true;
        setSeeing(false);
      }}
    />
  );

  if (home)
    return (
      <div className="ml-hc-confirm mothli-ending">
        <span>{endingNote(day)}</span>
        <button ref={seeRef} type="button" className="ml-hc-link" aria-haspopup="dialog" onClick={() => setSeeing(true)}>
          {SEE_THE_PLAN}
        </button>
        <button type="button" className="ml-hc-link ml-hc-keep" onClick={gotIt}>
          {GOT_IT}
        </button>
        {sheet}
      </div>
    );
  return (
    <div className="mothli-clear lp-clear mothli-ending">
      <span>{endingNote(day)}</span>
      <Button ref={seeRef} variant="ghost" aria-haspopup="dialog" onClick={() => setSeeing(true)}>
        {SEE_THE_PLAN}
      </Button>
      <Button variant="ghost" onClick={gotIt}>
        {GOT_IT}
      </Button>
      {sheet}
    </div>
  );
}
