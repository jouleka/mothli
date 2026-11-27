/** Hosted mode: what a reader sees of the gate (ADR 0045 section 6, ADR 0046).
 *
 * The invitation, for a reader who does not pay, in place of a panel's content (the chat in a
 * book, the home chat, the cast, "Story so far"): worded for a reader who has never had Mothli
 * (`new`) or for one whose plan ended (`plan`), with one button that opens the plan sheet.
 * Joining updates where the reader stands at once, and each panel comes back without a reload.
 *
 * A question the reader asks anyway is kept (`KeptQuestion`): their own words stay on screen, the
 * invitation speaks of their book and their place in it, and the question is sent once they join.
 *
 * No number of replies, no counter, no countdown, no price, never "unlimited". */
import { type RefObject, useEffect, useRef, useState } from "react";
import { api } from "../api";
import { PLAN_TITLE_NEW, PlanSheet } from "../shelf/PlanSheet";
import { Button } from "./Button";
import { Moth } from "./Moth";
import {
  mothliClosed,
  mothliEmail,
  type MothliStanding,
  noteMothliJoined,
  setMothliStanding,
} from "./mothliStanding";
import "./mothli-plan.css";

export const INVITATIONS: Record<"new" | "plan", string> = {
  new: "Hi, I’m Mothli! I’d love to read along with you. I only know the pages you’ve read, so no spoilers, promise. Join me on the plan and you can ask me anything. Your books, your place and your highlights stay free, always.",
  plan: "Your plan has ended, so I’m taking a little rest. Your books, your place and your highlights stay right here, free, always. Come back any time and we’ll pick up right where we left off.",
};
/** The invitation under a question the reader asked anyway: their book, by name, and their place
 * in it (the chapter they are in, as the footer names it), never anything past it. At home there
 * is no one book, so neither is named. */
export function keptInvitation(
  why: MothliStanding["why"],
  book: { title?: string | null; place?: string | null } = {},
): string {
  const pages = `the pages${book.title ? ` of ${book.title}` : ""} you’ve read so far${book.place ? `, up to your page in ${book.place}` : ""}`;
  return why === "plan"
    ? `Ooh, I’ve missed these! I’d love to answer, using only ${pages}. Come back to the plan and I’ll answer right away.`
    : `Ooh, good one! I’d love to answer, using only ${pages}. Join me on the plan and I’ll answer right away.`;
}
/** Under the question once the reader has said not now, and whenever they come back to it. */
export const KEPT_FOR_YOU = "I’ll keep this for you.";
/** Said to a screen reader when a question is kept, since focus stays in the box. */
export const KEPT_SAID = "Your question is kept, with a note from Mothli just above.";
export const KEEP_TALKING = "Keep talking with Mothli";
/** The invitation's button for a reader who has never talked with Mothli. */
export const START_TALKING = PLAN_TITLE_NEW;

/** The plan sheet as the invitations open it. Joining updates where the reader stands, and the
 * panel comes back in the invitation's place. Closed without joining: focus goes back to the
 * button, `onDeclined` is told, and a reader already on the plan from elsewhere (another tab,
 * another device) gets the panel back. */
export function usePlanSheet(onDeclined?: () => void) {
  const [seeing, setSeeing] = useState(false);
  const joined = useRef(false);
  const backToButton = useRef(false);
  const buttonRef = useRef<HTMLButtonElement>(null);

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
      onDeclined?.();
      void api.plan()
        .then((plan) => {
          if (plan.state === "paying" && mothliClosed()) setMothliStanding({ state: plan.state, why: plan.why });
        })
        .catch(() => { /* Nothing changes. */ });
    }
    setSeeing(false);
  };

  const sheet = seeing && (
    <PlanSheet
      email={mothliEmail()}
      onJoined={() => {
        joined.current = true;
      }}
      onClose={close}
    />
  );
  return { buttonRef, open: () => setSeeing(true), sheet };
}

/** The words take focus, so a screen reader reads them, when they replace content that held
 * focus (`replaced`) or when focus is outside `scope` (a dialog panel that just opened); never
 * from elsewhere on the page, nor on a page just loaded. */
function useReadOut(textRef: RefObject<HTMLElement>, replaced: boolean, scope?: RefObject<HTMLElement>) {
  useEffect(() => {
    const active = document.activeElement;
    const lost = !active || active === document.body;
    if ((replaced && lost) || (scope?.current && !scope.current.contains(active)))
      textRef.current?.focus({ preventScroll: true });
  }, []);
}

/** In place of a panel's content for a reader who does not pay. */
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
  const textRef = useRef<HTMLParagraphElement>(null);
  const { buttonRef, open, sheet } = usePlanSheet();
  useReadOut(textRef, replaced, scope);

  const ended = why === "plan";
  return (
    <div className={home ? "ml-hc-hello mothli-invite mothli-invite--home" : "memory-state mothli-invite"}>
      {!home && <Moth mood={ended ? "sleepy" : "curious"} size={64} />}
      <p ref={textRef} tabIndex={-1} className={home ? "ml-hc-hand" : "lp-voice"}>
        {INVITATIONS[ended ? "plan" : "new"]}
      </p>
      <div>
        <Button ref={buttonRef} variant="primary" aria-haspopup="dialog" onClick={open}>
          {ended ? KEEP_TALKING : START_TALKING}
        </Button>
      </div>
      {sheet}
    </div>
  );
}

/** The question a reader who does not pay asked anyway, kept: their own words as theirs in the
 * thread, Mothli's invitation about their book and their place in it, one button for the plan
 * sheet, and a way to edit or remove it. Once they have said not now (or come back to it later),
 * "I'll keep this for you." Nothing more is asked of them: no pop-up, no countdown. */
export function KeptQuestion({
  question,
  why,
  book,
  home = false,
  earlier = false,
  scope,
  onEdit,
  onRemove,
}: {
  question: string;
  why: MothliStanding["why"];
  book?: { title?: string | null; place?: string | null };
  /** The home chat's handwriting rather than a panel's. */
  home?: boolean;
  /** Kept in an earlier sitting: it is already waiting. */
  earlier?: boolean;
  scope?: RefObject<HTMLElement>;
  onEdit: () => void;
  onRemove: () => void;
}) {
  const [waiting, setWaiting] = useState(earlier);
  const textRef = useRef<HTMLParagraphElement>(null);
  const { buttonRef, open, sheet } = usePlanSheet(() => setWaiting(true));
  // Only when a dialog panel just opened on it: a question just asked leaves focus in the box.
  useReadOut(textRef, false, scope);

  const ended = why === "plan";
  const line = keptInvitation(why, book);
  const actions = (
    <div className="mothli-kept-actions">
      <div>
        <Button ref={buttonRef} variant="primary" aria-haspopup="dialog" onClick={open}>
          {ended ? KEEP_TALKING : START_TALKING}
        </Button>
      </div>
      {waiting && <p className="mothli-kept-note">{KEPT_FOR_YOU}</p>}
      <div className="mothli-kept-edit">
        <button type="button" onClick={onEdit}>Edit my question</button>
        <button type="button" onClick={onRemove}>Remove it</button>
      </div>
      {sheet}
    </div>
  );
  return home ? (
    <div className="ml-hc-thread mothli-kept" role="region" aria-label="Your question, kept">
      <ol className="ml-hc-turns">
        <li className="ml-hc-exchange" data-latest>
          <p className="ml-hc-you"><span className="sr-only">You: </span>{question}</p>
          <p ref={textRef} tabIndex={-1} className="ml-hc-reply" aria-live="polite">
            <span className="sr-only">Mothli: </span>{line}
          </p>
          {actions}
        </li>
      </ol>
    </div>
  ) : (
    <section className="mothli-exchange lp-exchange mothli-kept" aria-label="Your question, kept" data-latest-exchange>
      <p className="mothli-question lp-you">{question}</p>
      <div className="mothli-reply lp-reply">
        <div className="lp-reply-body">
          <p ref={textRef} tabIndex={-1} aria-live="polite">{line}</p>
          {actions}
        </div>
      </div>
    </section>
  );
}
