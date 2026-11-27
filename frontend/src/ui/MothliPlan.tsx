/** Hosted mode: what a reader sees of the gate (ADR 0045 section 6, ADR 0046).
 *
 * The invitation, for a reader who does not pay, in place of a panel's content (the chat in a
 * book, the home chat, the cast, "Story so far"): worded for a reader who has never had Mothli
 * (`new`) or for one whose plan ended (`plan`), with one button that opens the plan sheet.
 * Joining updates where the reader stands at once, and each panel comes back without a reload.
 *
 * No number, no counter, no countdown, never "unlimited". */
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
export const KEEP_TALKING = "Keep talking with Mothli";
/** The invitation's button for a reader who has never talked with Mothli. */
export const START_TALKING = PLAN_TITLE_NEW;

/** In place of a panel's content for a reader who does not pay. The text takes focus, so a
 * screen reader reads it, when it replaces content that held focus (`replaced`: the panel showed
 * its content before) or when focus is outside `scope` (a dialog panel that just opened); it
 * never takes focus from elsewhere on the page, nor on a page just loaded. */
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
          if (plan.state === "paying" && mothliClosed()) setMothliStanding({ state: plan.state, why: plan.why });
        })
        .catch(() => { /* Nothing changes. */ });
    }
    setSeeing(false);
  };

  const ended = why === "plan";
  return (
    <div className={home ? "ml-hc-hello mothli-invite mothli-invite--home" : "memory-state mothli-invite"}>
      {!home && <Moth mood={ended ? "sleepy" : "curious"} size={64} />}
      <p ref={textRef} tabIndex={-1} className={home ? "ml-hc-hand" : "lp-voice"}>
        {INVITATIONS[ended ? "plan" : "new"]}
      </p>
      <div>
        <Button ref={buttonRef} variant="primary" aria-haspopup="dialog" onClick={() => setSeeing(true)}>
          {ended ? KEEP_TALKING : START_TALKING}
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
