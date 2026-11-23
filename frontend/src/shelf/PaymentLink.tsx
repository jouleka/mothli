/** Hosted mode, signed out: Mothli's payment-link page (`/?_ptxn=txn_…`, ADR 0045 "One Paddle
 * account, several products"). Paddle's emails about a checkout a reader started (the reminder
 * after one left unpaid, say) bring them here, maybe on a device where they are not signed in.
 *
 * The checkout opens anyway, over the welcome. It cannot lose a payment: the transaction already
 * names the reader who started it and that checkout's token (its custom data, made by the server),
 * so a payment made here makes that reader paying whoever is signed in on this device, or no one.
 * Asking them to sign in first could: a sign-in that fails, or is made with another account, would
 * stand between a willing reader and their checkout. Afterwards they are told to sign in to see
 * their plan; the welcome's own sign-in button does the rest (SignIn.tsx).
 *
 * Paddle.js is fetched here only for a well-formed id (paddle.ts), with the public client token and
 * environment from `GET /api/billing/payment-link`, the one thing this page reads. */
import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { Button } from "../ui/Button";
import { listenToPaddle, loadPaddle } from "./paddle";
import { PLAN_LINES } from "./PlanSheet";
import "./account.css";

export const PAYMENT_LINK_LINES = {
  opening: PLAN_LINES.resuming,
  paying: PLAN_LINES.paying,
  paid: "Thank you! Sign in and you’ll find your plan waiting in Your account.",
  closed: "No worries, nothing has changed. Sign in and I’ll open your checkout again.",
  unreachable: PLAN_LINES.unreachable,
  tryAgain: PLAN_LINES.tryAgain,
} as const;

type Phase = "opening" | "paying" | "paid" | "closed" | "unreachable";

export function PaymentLink({ transaction, onPaid }: { transaction: string; onPaid: () => void }) {
  const [phase, setPhase] = useState<Phase>("opening");
  const [attempt, setAttempt] = useState(0);
  const phaseRef = useRef(phase);
  phaseRef.current = phase;
  const onPaidRef = useRef(onPaid);
  onPaidRef.current = onPaid;
  const messageRef = useRef<HTMLParagraphElement>(null);

  useEffect(() => {
    let stale = false;
    setPhase("opening");
    (async () => {
      try {
        const paddle = await loadPaddle(await api.paymentLink());
        if (stale) return;
        setPhase("paying");
        paddle.Checkout.open({ transactionId: transaction, settings: { displayMode: "overlay", locale: "en" } });
      } catch {
        if (!stale) setPhase("unreachable");
      }
    })();
    return () => { stale = true; };
  }, [transaction, attempt]);

  useEffect(
    () =>
      listenToPaddle((event) => {
        if (event.name === "checkout.completed" && phaseRef.current === "paying") {
          setPhase("paid");
          onPaidRef.current();
        } else if (event.name === "checkout.closed" && phaseRef.current === "paying") setPhase("closed");
      }),
    [],
  );

  // A new line after Paddle's checkout takes focus, so it is read out.
  useEffect(() => {
    if (phase === "paid" || phase === "closed" || phase === "unreachable") messageRef.current?.focus();
  }, [phase]);

  return (
    <div>
      <p
        ref={messageRef}
        tabIndex={-1}
        role="status"
        className="ml-welcome-note"
        aria-busy={phase === "opening" || undefined}
      >
        {PAYMENT_LINK_LINES[phase]}
      </p>
      {phase === "unreachable" && (
        <Button variant="secondary" onClick={() => setAttempt((count) => count + 1)}>
          {PAYMENT_LINK_LINES.tryAgain}
        </Button>
      )}
    </div>
  );
}
