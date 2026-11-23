/** Hosted mode: the plan part of the account sheet (ADR 0045 section 6), from the server's
 * `GET /api/billing/plan`. Where the reader stands, in a line, and what they can do about it:
 * "See the plan" (the plan sheet), "Update payment details" (Paddle's customer portal in a new
 * tab; the link is never kept), "Cancel my plan" with one confirmation, and "Keep my plan". The
 * cancel button is never hidden behind anything. A server that cannot open a checkout, or cannot
 * manage plans, shows the line and no button that would lead nowhere. No amount, no count. */
import { useEffect, useId, useRef, useState, type RefObject } from "react";
import { api, ApiError, type Plan } from "../api";
import { Button } from "../ui/Button";
import "./home-chat.css";
import "./account.css";

export const ACCOUNT_PLAN_LINES = {
  loading: "Looking up your plan…",
  failed: "I couldn’t look up your plan just now. Try again in a moment.",
  new: "Talking with Mothli is free for your first two weeks, starting the first time you use it.",
  free: "You’re in your free weeks with Mothli.",
  endedTime: "Your two free weeks with Mothli are over. Your books stay free, always.",
  endedCeiling: "Your free start with Mothli is all used up. Your books stay free, always.",
  endedPlan: "Your plan has ended. Your books stay free, always.",
  onPlan: "You’re on the plan.",
  retrying: "Your last payment didn’t go through. Paddle will try again, or you can update your card here.",
  wontRenew: "Your plan won’t renew.",
  gift: "Mothli is on the house for you. Enjoy!",
  giftAndPlan: "You have a plan of your own too.",
  seePlan: "See the plan",
  update: "Update payment details",
  opening: "Opening…",
  openPortal: "Open your payment details",
  cancel: "Cancel my plan",
  cancelling: "Cancelling…",
  keep: "Keep my plan",
  keeping: "One moment…",
  confirmNoDate: "Cancel your plan? You won’t be charged again. Your books stay yours, always.",
  failedAction: "I couldn’t do that just now. Check your connection and try again.",
} as const;
export const freeEnds = (day: string) => `Your free weeks end on ${day}.`;
export const renews = (kind: "monthly" | "yearly", day: string | null) =>
  day ? `You’re on the ${kind} plan. It renews on ${day}.` : `You’re on the ${kind} plan.`;
export const planEnds = (day: string) => `Your plan ends on ${day}.`;
export const confirmCancel = (day: string) =>
  `Cancel your plan? I’ll stay with you until ${day}, and you won’t be charged again. Your books stay yours, always.`;
/** The server's own refusals of a change, each with a kind line; nothing on screen changes. */
export const PLAN_REFUSED: Record<string, string> = {
  try_again_soon: "Your plan is renewing right now. Please try again in a few minutes.",
  paddle_unavailable: "I can’t reach our payment service just now, so nothing has changed. Please try again in a minute.",
};

/** A day in the reader's own locale and time zone: "3 December" (the year only when it is not
 * this one). */
export function planDay(iso: string, now = new Date(), locale?: string, timeZone?: string): string {
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) return "";
  const year = (date: Date) => new Intl.DateTimeFormat("en", { year: "numeric", timeZone }).format(date);
  return when.toLocaleDateString(locale, {
    day: "numeric",
    month: "long",
    ...(year(when) !== year(now) ? { year: "numeric" } : {}),
    timeZone,
  });
}

function refusal(reason: unknown): string {
  return (reason instanceof ApiError && reason.code && PLAN_REFUSED[reason.code]) || ACCOUNT_PLAN_LINES.failedAction;
}

export function YourPlan({
  plan,
  onPlan,
  onSeePlan,
  seePlanRef,
}: {
  plan: Plan | "loading" | "failed";
  onPlan: (plan: Plan) => void;
  onSeePlan: () => void;
  seePlanRef: RefObject<HTMLButtonElement>;
}) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState<"cancel" | "keep" | "portal" | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  // Only where the browser would not open a tab: the link, until it is followed.
  const [portalLink, setPortalLink] = useState<string | null>(null);
  const questionRef = useRef<HTMLParagraphElement>(null);
  const lineRef = useRef<HTMLParagraphElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const focusNext = useRef<(() => void) | null>(null);
  useEffect(() => {
    const move = focusNext.current;
    focusNext.current = null;
    move?.();
  });
  const questionId = useId();

  if (plan === "loading" || plan === "failed")
    return (
      <div className="ml-account-part ml-account-plan">
        <p className="ml-mem-quiet" role="status">
          {plan === "loading" ? ACCOUNT_PLAN_LINES.loading : ACCOUNT_PLAN_LINES.failed}
        </p>
      </div>
    );

  const day = plan.next_date ? planDay(plan.next_date) : null;
  const gift = plan.plan === "gift";
  const own = plan.subscribed && plan.manage;
  const canJoin = plan.checkout !== null && !plan.subscribed && plan.state !== "paying";

  const lines: string[] = [];
  if (plan.state === "new") lines.push(ACCOUNT_PLAN_LINES.new);
  else if (plan.state === "free") lines.push(day ? freeEnds(day) : ACCOUNT_PLAN_LINES.free);
  else if (plan.state === "ended")
    lines.push(plan.why === "ceiling" ? ACCOUNT_PLAN_LINES.endedCeiling
      : plan.why === "plan" ? ACCOUNT_PLAN_LINES.endedPlan : ACCOUNT_PLAN_LINES.endedTime);
  else if (gift) {
    lines.push(ACCOUNT_PLAN_LINES.gift);
    if (plan.subscribed)
      lines.push(plan.payment_retrying ? ACCOUNT_PLAN_LINES.retrying
        : plan.cancel_scheduled ? ACCOUNT_PLAN_LINES.wontRenew : ACCOUNT_PLAN_LINES.giftAndPlan);
  } else if (plan.payment_retrying) lines.push(ACCOUNT_PLAN_LINES.retrying);
  else if (plan.cancel_scheduled) lines.push(day ? planEnds(day) : ACCOUNT_PLAN_LINES.wontRenew);
  else lines.push(plan.plan === "monthly" || plan.plan === "yearly" ? renews(plan.plan, day) : ACCOUNT_PLAN_LINES.onPlan);

  const change = async (cancel: boolean) => {
    setBusy(cancel ? "cancel" : "keep");
    setProblem(null);
    try {
      const now = cancel ? await api.cancelPlan() : await api.keepPlan();
      setConfirming(false);
      focusNext.current = () => lineRef.current?.focus();
      onPlan(now);
    } catch (reason) {
      setProblem(refusal(reason));
    } finally {
      setBusy(null);
    }
  };

  const portal = async () => {
    setBusy("portal");
    setProblem(null);
    setPortalLink(null);
    // Opened now, while the tap still counts, so the browser does not block it; pointed at
    // Paddle once the server answers. The link is never kept.
    const tab = window.open("", "_blank");
    if (tab) tab.opener = null;
    try {
      const { url } = await api.planPortal();
      if (tab) tab.location.href = url;
      else setPortalLink(url);
    } catch (reason) {
      tab?.close();
      setProblem(refusal(reason));
    } finally {
      setBusy(null);
    }
  };

  const ask = () => {
    setProblem(null);
    setConfirming(true);
    focusNext.current = () => questionRef.current?.focus();
  };
  const back = () => {
    setProblem(null);
    setConfirming(false);
    focusNext.current = () => cancelRef.current?.focus();
  };

  return (
    <div className="ml-account-part ml-account-plan">
      {confirming ? (
        <>
          <p className="ml-account-who" id={questionId} ref={questionRef} tabIndex={-1}>
            {day && !gift ? confirmCancel(day) : ACCOUNT_PLAN_LINES.confirmNoDate}
          </p>
          {problem && <p className="fl-error" role="alert">{problem}</p>}
          <div className="ml-pending-actions" role="group" aria-labelledby={questionId}>
            <Button variant="secondary" disabled={busy !== null} onClick={back}>
              {ACCOUNT_PLAN_LINES.keep}
            </Button>
            <Button
              variant="danger"
              disabled={busy !== null}
              aria-busy={busy === "cancel" || undefined}
              onClick={() => void change(true)}
            >
              {busy === "cancel" ? ACCOUNT_PLAN_LINES.cancelling : ACCOUNT_PLAN_LINES.cancel}
            </Button>
          </div>
        </>
      ) : (
        <>
          {lines.map((line, at) => (
            <p key={line} className="ml-account-who" ref={at === 0 ? lineRef : undefined} tabIndex={at === 0 ? -1 : undefined}>
              {line}
            </p>
          ))}
          {problem && <p className="fl-error" role="alert">{problem}</p>}
          {portalLink && (
            <a className="ml-hc-link" href={portalLink} target="_blank" rel="noopener noreferrer" onClick={() => setPortalLink(null)}>
              {ACCOUNT_PLAN_LINES.openPortal}
            </a>
          )}
          {(canJoin || own) && (
            <div className="ml-pending-actions">
              {canJoin && (
                <Button ref={seePlanRef} variant="primary" aria-haspopup="dialog" onClick={onSeePlan}>
                  {ACCOUNT_PLAN_LINES.seePlan}
                </Button>
              )}
              {own && plan.cancel_scheduled && (
                <Button
                  variant="primary"
                  disabled={busy !== null}
                  aria-busy={busy === "keep" || undefined}
                  onClick={() => void change(false)}
                >
                  {busy === "keep" ? ACCOUNT_PLAN_LINES.keeping : ACCOUNT_PLAN_LINES.keep}
                </Button>
              )}
              {own && !plan.cancel_scheduled && (
                <>
                  <Button
                    variant={plan.payment_retrying ? "primary" : "secondary"}
                    disabled={busy !== null}
                    aria-busy={busy === "portal" || undefined}
                    onClick={() => void portal()}
                  >
                    {busy === "portal" ? ACCOUNT_PLAN_LINES.opening : ACCOUNT_PLAN_LINES.update}
                  </Button>
                  <Button ref={cancelRef} variant="secondary" disabled={busy !== null} onClick={ask}>
                    {ACCOUNT_PLAN_LINES.cancel}
                  </Button>
                </>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}
