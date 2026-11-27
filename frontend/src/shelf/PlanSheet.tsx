/** Hosted mode: the plan sheet, "Keep talking with Mothli" (ADR 0045 section 6). The two plans with
 * the prices Paddle gives this reader (yearly first, as a month, with its saving), then Paddle's
 * checkout over the page for a transaction the server made. When the checkout completes or closes
 * nothing is assumed: the sheet asks the server for the plan until it says the reader is on it,
 * for a bounded time. After a completed payment that has not arrived in that time it says the
 * payment may still be on its way and keeps watching quietly (../ui/latePlan.ts), so Mothli opens
 * up by itself when it comes; after a checkout only closed, a short check (in case the completed
 * event was missed), then the two plans again with a calm line.
 *
 * Opened from the account sheet, and from anywhere else that renders it: `<PlanSheet email=…
 * onClose=… />` (it reads the plan itself unless given one). A modal sheet like "What Mothli
 * remembers" (fl-scrim / fl-sheet), with the same focus handling; the opener puts focus back.
 * Paddle.js is fetched only when this sheet opens (./paddle.ts). Given a `transaction` (Paddle's
 * payment link, `/?_ptxn=…`, read by SignIn.tsx's gate), it opens that checkout at once instead. */
import { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api, ApiError, type Plan } from "../api";
import { keepTabFocusInside } from "../reader/focusTrap";
import { Button } from "../ui/Button";
import { Icon } from "../ui/Icon";
import { claimLatePlan, watchForLatePlan } from "../ui/latePlan";
import { mothliStanding } from "../ui/mothliStanding";
import { monthlyTotal, yearlyMonth, yearlyTotal } from "../ui/planFigures";
import { PAGE_PATHS } from "../public/pages";
import { listenToPaddle, loadPaddle, planPrices, type PaddleLike, type PlanPrices } from "./paddle";
import "../library/free-library.css";
import "./home-chat.css";
import "./account.css";

export const PLAN_TITLE = "Keep talking with Mothli";
/** For a reader who has never used Mothli: there is nothing yet to keep talking about. */
export const PLAN_TITLE_NEW = "Talk with Mothli";
export const PLAN_LINES = {
  loading: "Getting the prices for you…",
  resuming: "One moment, I’m opening your checkout…",
  notOffered: "The plan isn’t on offer here just yet. Your books stay free either way.",
  already: "Good news: you’re already on the plan! There’s nothing more to do.",
  unreachable:
    "I couldn’t get the checkout ready just now. It might be your connection, or something in your browser blocking it.",
  tryAgain: "Try again",
  yearly: "Yearly",
  monthly: "Monthly",
  bestValue: "Best value",
  chooseYearly: "Choose yearly",
  chooseMonthly: "Choose monthly",
  starting: "One moment…",
  // The owner's soft brake (27 November 2026), in words, never numbers.
  rhythm: "I keep a gentle daily rhythm, so on a very busy day I might ask to pick up again tomorrow.",
  cancelAnyTime: "Cancel any time from Your account. Your books stay free either way.",
  paddle: "Payments are handled by Paddle, who sells Mothli on our behalf.",
  paying: "Your checkout is open. Take your time, I’ll be right here.",
  waiting: "One moment, I’m getting ready…",
  checking: "One moment…",
  nothingChanged: "No worries, nothing has changed. Pick a plan whenever you like.",
  joined: "You’re on the plan! I’m so glad we get to keep talking.",
  done: "Done",
  // True while ../ui/latePlan.ts keeps watching, which it does for a bounded time.
  slow:
    "Your payment may still be on its way to me. That can take a few minutes. There’s nothing you need to do: as soon as it arrives, I’ll open up right here by myself.",
  close: "Close",
} as const;
/** The server's refusals of a checkout, each in Mothli's voice. */
export const CHECKOUT_REFUSED: Record<string, string> = {
  checkout_busy: "We’ve opened a lot of checkouts today, so let’s take a little break. Please try again later.",
  plan_not_sold: "That plan isn’t on offer right now. Sorry about that!",
  paddle_unavailable: "I can’t reach our payment service just now, so nothing has changed. Please try again in a minute.",
};
export const CHECKOUT_FAILED = "I couldn’t start the checkout just now. Check your connection and try again.";

export { monthlyTotal, yearlyMonth, yearlyTotal };

// After the checkout, how often and how long to ask the server whether the plan has arrived.
// A payment Paddle has completed gets the longer wait; a checkout only closed, a short check in
// case the completed event was missed.
export const ASK_EVERY_MS = 2_000;
export const WAIT_AFTER_PAYMENT_MS = 60_000;
export const WAIT_AFTER_CLOSING_MS = 8_000;

type Phase =
  | "loading" | "notOffered" | "already" | "unreachable" | "choose"
  | "paying" | "checking" | "waiting" | "joined" | "slow";

export function PlanSheet({
  email,
  plan: given,
  transaction,
  onClose,
  onJoined,
}: {
  /** Prefilled in Paddle's checkout; the reader can change it there. */
  email?: string | null;
  /** The plan, when the opener already has it; otherwise the sheet asks. */
  plan?: Plan;
  /** A checkout the reader started earlier, from Paddle's payment link (`_ptxn`): opened at once,
   * unless they are on the plan already, with the same wait after it. */
  transaction?: string;
  onClose: () => void;
  onJoined?: (plan: Plan) => void;
}) {
  const [phase, setPhaseState] = useState<Phase>("loading");
  // The phase as it is now, for Paddle's listener: set together with the state, never from a
  // render, so two events in the same tick see each other (a "closed" right after a "completed"
  // must not read "paying" and undo the wait for the payment).
  const phaseRef = useRef<Phase>("loading");
  const setPhase = (next: Phase) => {
    phaseRef.current = next;
    setPhaseState(next);
  };
  const [resume, setResume] = useState(transaction ?? null);
  const [plan, setPlan] = useState<Plan | null>(given ?? null);
  const [prices, setPrices] = useState<PlanPrices | null>(null);
  const [starting, setStarting] = useState<"monthly" | "yearly" | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  // Back at the plans after a checkout closed with nothing paid: the calm line says so.
  const [unchanged, setUnchanged] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const paddleRef = useRef<PaddleLike | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const messageRef = useRef<HTMLParagraphElement>(null);
  const yearlyRef = useRef<HTMLButtonElement>(null);
  const monthlyRef = useRef<HTMLButtonElement>(null);
  // After a refused checkout, the choice pressed takes focus back once it is enabled again.
  const refocus = useRef<"monthly" | "yearly" | null>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const onJoinedRef = useRef(onJoined);
  onJoinedRef.current = onJoined;
  const titleId = useId();
  const yearlyId = useId();
  const monthlyId = useId();

  // A modal: the page behind is inert, Escape closes it, Tab stays inside. While Paddle's
  // checkout is open over it, the keyboard is Paddle's.
  useEffect(() => {
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const siblings = Array.from(document.body.children)
      .filter((element) => element !== rootRef.current)
      .map((element) => ({ element: element as HTMLElement, inert: (element as HTMLElement).inert }));
    for (const { element } of siblings) element.inert = true;
    closeRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (phaseRef.current === "paying") return;
      if (event.key === "Escape") {
        event.preventDefault();
        onCloseRef.current();
      } else keepTabFocusInside(rootRef.current, event);
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
      for (const { element, inert } of siblings) element.inert = inert;
    };
  }, []);

  // The plan, then Paddle.js and the prices. Paddle's script is asked for here and nowhere else.
  useEffect(() => {
    let stale = false;
    setPhase("loading");
    (async () => {
      let current = plan;
      try {
        current ??= await api.plan();
      } catch {
        if (!stale) setPhase("unreachable");
        return;
      }
      if (stale) return;
      setPlan(current);
      if (current.subscribed) return setPhase("already");
      const setup = current.checkout;
      if (!setup) return setPhase("notOffered");
      try {
        const paddle = await loadPaddle(setup);
        if (stale) return;
        if (resume) {
          // The checkout from the payment link, once; the prices only for afterwards.
          setResume(null);
          paddleRef.current = paddle;
          openCheckout(paddle, resume);
          planPrices(paddle, setup.prices).then((found) => { if (!stale) setPrices(found); }, () => {});
          return;
        }
        const found = await planPrices(paddle, setup.prices);
        if (stale) return;
        paddleRef.current = paddle;
        setPrices(found);
        setPhase("choose");
      } catch {
        if (!stale) setPhase("unreachable");
      }
    })();
    return () => { stale = true; };
    // Run once, and again from the start for each "Try again".
  }, [attempt]);

  // Paddle's checkout events: a completed payment starts the wait, a closed checkout the short
  // check; nothing more is assumed. Once a payment is reported completed, a closed checkout never
  // takes the sheet back, in whichever order or however close together the two arrive.
  useEffect(
    () =>
      listenToPaddle((event) => {
        const now = phaseRef.current;
        if (event.name === "checkout.completed" && (now === "paying" || now === "checking" || now === "choose"))
          setPhase("waiting");
        else if (event.name === "checkout.closed" && now === "paying") setPhase("checking");
      }),
    [],
  );

  // The wait: ask the server for the plan until it says the reader is on it, or time runs out.
  // Out of time after a payment: where to look. After a checkout only closed: the plans again.
  useEffect(() => {
    if (phase !== "waiting" && phase !== "checking") return;
    let stale = false;
    let timer = 0;
    const paid = phase === "waiting";
    const until = Date.now() + (paid ? WAIT_AFTER_PAYMENT_MS : WAIT_AFTER_CLOSING_MS);
    const ask = async () => {
      try {
        const now = await api.plan();
        if (stale) return;
        if (now.subscribed) {
          setPlan(now);
          setPhase("joined");
          onJoinedRef.current?.(now);
          return;
        }
      } catch {
        // A passing failure: ask again.
      }
      if (stale) return;
      if (Date.now() + ASK_EVERY_MS > until) {
        if (paid) setPhase("slow");
        else {
          setUnchanged(true);
          setPhase("choose");
        }
      } else timer = window.setTimeout(() => void ask(), ASK_EVERY_MS);
    };
    timer = window.setTimeout(() => void ask(), ASK_EVERY_MS);
    return () => {
      stale = true;
      window.clearTimeout(timer);
    };
  }, [phase]);

  // Out of time after a payment: the quiet watch (../ui/latePlan.ts) goes on asking, past this
  // sheet. While the sheet is open it hears the news first and says it here.
  useEffect(() => {
    if (phase !== "slow") return;
    watchForLatePlan();
    return claimLatePlan((now) => {
      if (phaseRef.current !== "slow") return false;
      setPlan(now);
      setPhase("joined");
      onJoinedRef.current?.(now);
      return true;
    });
  }, [phase]);

  useEffect(() => {
    if (starting || !refocus.current) return;
    (refocus.current === "yearly" ? yearlyRef : monthlyRef).current?.focus();
    refocus.current = null;
  }, [starting]);

  // A new message in the sheet's own words takes focus, so it is read out.
  useEffect(() => {
    if (["notOffered", "already", "unreachable", "checking", "waiting", "joined", "slow"].includes(phase)
      || (phase === "choose" && unchanged))
      messageRef.current?.focus();
  }, [phase, unchanged]);

  const choose = async (which: "monthly" | "yearly") => {
    if (starting || !paddleRef.current) return;
    setStarting(which);
    setProblem(null);
    setUnchanged(false);
    let transaction: string;
    try {
      transaction = (await api.startCheckout(which)).transaction_id;
    } catch (reason) {
      if (reason instanceof ApiError && reason.code === "already_subscribed") setPhase("already");
      else {
        setProblem(reason instanceof ApiError && reason.code && CHECKOUT_REFUSED[reason.code] || CHECKOUT_FAILED);
        // The button was disabled while waiting, which drops focus to the page: put it back.
        refocus.current = which;
      }
      setStarting(null);
      return;
    }
    openCheckout(paddleRef.current, transaction);
    setStarting(null);
  };

  function openCheckout(paddle: PaddleLike, transaction: string) {
    try {
      setPhase("paying");
      paddle.Checkout.open({
        transactionId: transaction,
        ...(email ? { customer: { email } } : {}),
        settings: { displayMode: "overlay", locale: "en" },
      });
    } catch {
      setPhase("unreachable");
    }
  }

  const message =
    phase === "loading" ? (resume ? PLAN_LINES.resuming : PLAN_LINES.loading)
    : phase === "notOffered" ? PLAN_LINES.notOffered
    : phase === "already" ? PLAN_LINES.already
    : phase === "unreachable" ? PLAN_LINES.unreachable
    : phase === "paying" ? PLAN_LINES.paying
    : phase === "checking" ? PLAN_LINES.checking
    : phase === "waiting" ? PLAN_LINES.waiting
    : phase === "joined" ? PLAN_LINES.joined
    : phase === "slow" ? PLAN_LINES.slow
    : phase === "choose" && unchanged ? PLAN_LINES.nothingChanged
    : "";
  const notStarted = (plan?.state ?? mothliStanding()?.state) === "new";

  return createPortal(
    <div
      className="fl-scrim"
      ref={rootRef}
      onPointerDown={(event) => event.target === event.currentTarget && phase !== "paying" && onClose()}
    >
      <div className="fl-sheet ml-mem ml-account ml-plan" role="dialog" aria-modal="true" aria-labelledby={titleId}>
        <button ref={closeRef} type="button" className="fl-sheet-close" aria-label="Close" onClick={onClose}>
          <Icon name="close" size={18} />
        </button>
        <div className="ml-mem-body">
          <h2 id={titleId}>{notStarted ? PLAN_TITLE_NEW : PLAN_TITLE}</h2>
          {/* Always on the page, so a screen reader hears each new line. */}
          <p
            ref={messageRef}
            tabIndex={-1}
            role="status"
            className={message ? "ml-plan-message" : "ml-plan-message ml-plan-message--empty"}
            aria-busy={phase === "loading" || phase === "checking" || phase === "waiting" || undefined}
          >
            {message}
          </p>

          {phase === "choose" && prices && (
            <>
              <ul className="ml-plan-choices">
                {prices.yearly && (
                  <li className="ml-plan-choice ml-plan-choice--yearly">
                    <h3>
                      {PLAN_LINES.yearly} <span className="ml-plan-tag">{PLAN_LINES.bestValue}</span>
                    </h3>
                    <div id={yearlyId}>
                      <p className="ml-plan-price">
                        {prices.yearly.perMonth ? yearlyMonth(prices.yearly.perMonth) : `${prices.yearly.total} a year`}
                      </p>
                      {(prices.yearly.perMonth || prices.yearly.saving !== null) && (
                        <p className="ml-mem-quiet">
                          {prices.yearly.perMonth
                            ? yearlyTotal(prices.yearly.total, prices.yearly.saving)
                            : `${prices.yearly.saving}% less than paying monthly.`}
                        </p>
                      )}
                    </div>
                    <Button
                      ref={yearlyRef}
                      variant="primary"
                      aria-describedby={yearlyId}
                      disabled={starting !== null}
                      aria-busy={starting === "yearly" || undefined}
                      onClick={() => void choose("yearly")}
                    >
                      {starting === "yearly" ? PLAN_LINES.starting : PLAN_LINES.chooseYearly}
                    </Button>
                  </li>
                )}
                {prices.monthly && (
                  <li className="ml-plan-choice">
                    <h3>{PLAN_LINES.monthly}</h3>
                    <p className="ml-plan-price" id={monthlyId}>{monthlyTotal(prices.monthly.total)}</p>
                    <Button
                      ref={monthlyRef}
                      variant={prices.yearly ? "secondary" : "primary"}
                      aria-describedby={monthlyId}
                      disabled={starting !== null}
                      aria-busy={starting === "monthly" || undefined}
                      onClick={() => void choose("monthly")}
                    >
                      {starting === "monthly" ? PLAN_LINES.starting : PLAN_LINES.chooseMonthly}
                    </Button>
                  </li>
                )}
              </ul>
              {problem && <p className="fl-error" role="alert">{problem}</p>}
              <p className="ml-mem-quiet">{PLAN_LINES.rhythm}</p>
              <p className="ml-mem-quiet">{PLAN_LINES.cancelAnyTime}</p>
              <p className="ml-mem-quiet">{PLAN_LINES.paddle}</p>
              {/* Open point 8, decided: no refund promise of Mothli's own, Paddle's terms only.
                  The pages say so; they open beside the sheet, so it stays as it is. */}
              <p className="ml-mem-quiet">
                Before you join:{" "}
                <a href={PAGE_PATHS.terms} target="_blank" rel="noopener">our terms</a>,{" "}
                <a href={PAGE_PATHS.refunds} target="_blank" rel="noopener">how refunds work</a> and{" "}
                <a href={PAGE_PATHS.privacy} target="_blank" rel="noopener">your privacy</a>.
              </p>
            </>
          )}

          {phase === "unreachable" && (
            <div>
              <Button variant="primary" onClick={() => setAttempt((count) => count + 1)}>
                {PLAN_LINES.tryAgain}
              </Button>
            </div>
          )}
          {phase === "joined" && (
            <div>
              <Button variant="primary" onClick={onClose}>{PLAN_LINES.done}</Button>
            </div>
          )}
          {phase === "slow" && (
            <div>
              <Button variant="primary" onClick={onClose}>{PLAN_LINES.close}</Button>
            </div>
          )}
        </div>
      </div>
    </div>,
    document.body,
  );
}
