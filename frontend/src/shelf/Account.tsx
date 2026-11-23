/** Hosted mode only: the reader's account, opened from the shelf's settings menu. Who is signed
 * in, "What Mothli remembers" when the server keeps recollections (not shown where it answers
 * 404), signing out, which also clears the books saved for offline reading on this device, and,
 * quietly at the end, deleting the account: a second step says plainly what happens and needs a
 * deliberate yes. A modal sheet like "What Mothli remembers" (fl-scrim / fl-sheet), with the same
 * focus handling. "Take a copy of my data" sits quietly after "What Mothli remembers", and is
 * offered again in the delete confirmation (TakeACopy.tsx). The reader's plan comes first, after
 * their name (YourPlan.tsx), where the server has one to tell; "See the plan" opens the plan sheet
 * over this one (PlanSheet.tsx), as "What Mothli remembers" does. */
import { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api, ApiError, type Plan } from "../api";
import { offersChat, type OfflineSession } from "../pwa/offline";
import { keepTabFocusInside } from "../reader/focusTrap";
import { Button } from "../ui/Button";
import { Icon } from "../ui/Icon";
import { noteMothliJoined } from "../ui/mothliStanding";
import { MothliRemembers } from "./MothliRemembers";
import { PlanSheet } from "./PlanSheet";
import { PAGE_PATHS } from "../public/pages";
import { TakeACopy } from "./TakeACopy";
import { noteAccountDeleted } from "./SignIn";
import { YourPlan } from "./YourPlan";
import "../library/free-library.css";
import "./home-chat.css";
import "./account.css";

const SIGN_OUT_FAILED = "I couldn’t finish signing you out. Check your connection and try again.";
const DELETE_FAILED = "I couldn’t do that just now. Check your connection and try again.";

export function Account({ session, onClose }: { session: OfflineSession; onClose: () => void }) {
  // null until the server answers; false where it keeps no recollections (404).
  const [remembers, setRemembers] = useState<boolean | null>(null);
  const [remembering, setRemembering] = useState(false);
  // The reader's plan; null where the server has none to tell (404).
  const [plan, setPlan] = useState<Plan | "loading" | "failed" | null>("loading");
  const [seeingPlan, setSeeingPlan] = useState(false);
  const [signingOut, setSigningOut] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  // Deleting: the second step, the reader's "I understand", and the request on its way.
  const [confirming, setConfirming] = useState(false);
  const [understood, setUnderstood] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const rememberRef = useRef<HTMLButtonElement>(null);
  const seePlanRef = useRef<HTMLButtonElement>(null);
  const returnToPlan = useRef(false);
  const deleteRef = useRef<HTMLButtonElement>(null);
  const confirmTitleRef = useRef<HTMLHeadingElement>(null);
  const returnToRemember = useRef(false);
  const returnToDelete = useRef(false);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const titleId = useId();
  const leaveId = useId();
  const understoodId = useId();

  // Into the second step, focus its question; back out of it, focus the door it came from.
  useEffect(() => {
    if (confirming) confirmTitleRef.current?.focus();
    else if (returnToDelete.current) {
      returnToDelete.current = false;
      deleteRef.current?.focus();
    }
  }, [confirming]);

  useEffect(() => {
    // A server without Mothli's chat keeps nothing to remember: no door, and nothing asked.
    if (!offersChat(session)) {
      setRemembers(false);
      return;
    }
    let stale = false;
    api.recollections()
      .then(() => { if (!stale) setRemembers(true); })
      .catch((reason) => {
        // Only a server without recollections hides the door; a passing failure keeps it, and
        // the sheet behind it says what went wrong.
        if (!stale) setRemembers(!(reason instanceof ApiError && reason.status === 404));
      });
    return () => { stale = true; };
  }, []);

  const readPlan = (quietly = false) => {
    if (!quietly) setPlan("loading");
    return api.plan()
      .then((found) => setPlan(found))
      .catch((reason) => {
        if (reason instanceof ApiError && reason.status === 404) setPlan(null);
        else if (!quietly) setPlan("failed");
      });
  };
  useEffect(() => {
    void readPlan();
  }, []);

  // A modal: the page behind is inert, Escape closes it, Tab stays inside. While "What Mothli
  // remembers" or the plan sheet is open on top, that sheet owns the keyboard instead.
  useEffect(() => {
    if (remembering || seeingPlan) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const siblings = Array.from(document.body.children)
      .filter((element) => element !== rootRef.current)
      .map((element) => ({ element: element as HTMLElement, inert: (element as HTMLElement).inert }));
    for (const { element } of siblings) element.inert = true;
    if (returnToRemember.current) {
      returnToRemember.current = false;
      rememberRef.current?.focus();
    } else if (returnToPlan.current && seePlanRef.current) seePlanRef.current.focus();
    else closeRef.current?.focus();
    returnToPlan.current = false;
    const onKeyDown = (event: KeyboardEvent) => {
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
  }, [remembering, seeingPlan]);

  const signOut = async () => {
    setSigningOut(true);
    setProblem(null);
    try {
      await api.logout();
      // Back to the welcome, which offers signing in again.
      window.location.assign("/");
    } catch {
      setProblem(SIGN_OUT_FAILED);
      setSigningOut(false);
    }
  };

  const deleteAccount = async () => {
    setDeleting(true);
    setProblem(null);
    try {
      const { erase_after } = await api.deleteAccount();
      // Signed out everywhere: back to the welcome, which says it is done and names the day.
      noteAccountDeleted(erase_after);
      window.location.assign("/");
    } catch {
      setProblem(DELETE_FAILED);
      setDeleting(false);
    }
  };

  const goBack = () => {
    returnToDelete.current = true;
    setProblem(null);
    setUnderstood(false);
    setConfirming(false);
  };

  if (seeingPlan)
    return (
      <PlanSheet
        email={session.user.email}
        plan={plan !== null && typeof plan === "object" ? plan : undefined}
        onJoined={(joined) => {
          setPlan(joined);
          // The chats, the cast and "Story so far" open again at once, without a reload.
          noteMothliJoined();
        }}
        onClose={() => {
          returnToPlan.current = true;
          setSeeingPlan(false);
          void readPlan(true);
        }}
      />
    );

  if (remembering)
    return (
      <MothliRemembers
        onClose={() => {
          returnToRemember.current = true;
          setRemembering(false);
        }}
      />
    );

  return createPortal(
    <div className="fl-scrim" ref={rootRef} onPointerDown={(event) => event.target === event.currentTarget && onClose()}>
      <div className="fl-sheet ml-mem ml-account" role="dialog" aria-modal="true" aria-labelledby={titleId}>
        <button ref={closeRef} type="button" className="fl-sheet-close" aria-label="Close" onClick={onClose}>
          <Icon name="close" size={18} />
        </button>
        {confirming ? (
          <div className="ml-mem-body">
            <h2 id={titleId} ref={confirmTitleRef} tabIndex={-1}>Delete your account?</h2>
            <p className="ml-account-who">Here’s what happens if you do.</p>
            <ul className="ml-account-steps">
              <li>You’re signed out everywhere, on every device, straight away.</li>
              <li>
                Everything is kept for 30 days, in case you change your mind. Sign in again before
                then and you can keep your account.
              </li>
              <li>
                After that, your books, your place in them, your conversations and what Mothli
                remembers are erased for good.
              </li>
            </ul>
            <TakeACopy what="Before you go, you can keep a copy of your shelf, your notes and your chats. The books themselves aren’t in it." />
            <label className="ml-account-understood" htmlFor={understoodId}>
              <input
                id={understoodId}
                type="checkbox"
                checked={understood}
                disabled={deleting}
                onChange={(event) => setUnderstood(event.target.checked)}
              />
              <span>I understand that after 30 days this can’t be undone.</span>
            </label>
            {problem && <p className="fl-error" role="alert">{problem}</p>}
            <div className="ml-pending-actions">
              <Button variant="secondary" disabled={deleting} onClick={goBack}>
                Go back
              </Button>
              <Button
                variant="danger"
                disabled={!understood || deleting}
                aria-busy={deleting || undefined}
                onClick={() => void deleteAccount()}
              >
                {deleting ? "Deleting…" : "Delete my account"}
              </Button>
            </div>
          </div>
        ) : (
          <div className="ml-mem-body">
            <h2 id={titleId}>Your account</h2>
            <p className="ml-account-who">
              You’re signed in as <strong>{session.user.display_name}</strong>.
            </p>

            {plan !== null && (
              <YourPlan
                plan={plan}
                onPlan={setPlan}
                onSeePlan={() => setSeeingPlan(true)}
                seePlanRef={seePlanRef}
              />
            )}

            {remembers && (
              <div className="ml-account-part">
                <button
                  ref={rememberRef}
                  type="button"
                  className="ml-hc-link ml-account-remember"
                  aria-haspopup="dialog"
                  onClick={() => setRemembering(true)}
                >
                  What Mothli remembers
                </button>
                <p className="ml-mem-quiet">The little things you’ve told Mothli about yourself. Forget any of them whenever you like.</p>
              </div>
            )}

            <div className="ml-account-part">
              <TakeACopy />
            </div>

            <div className="ml-account-part">
              <p className="ml-mem-quiet" id={leaveId}>
                Signing out also clears the books you saved for offline reading on this device. Your shelf
                and your place stay with your account, ready for when you sign back in.
              </p>
              {problem && <p className="fl-error" role="alert">{problem}</p>}
              <div>
                <Button
                  variant="secondary"
                  aria-describedby={leaveId}
                  disabled={signingOut}
                  aria-busy={signingOut || undefined}
                  onClick={() => void signOut()}
                >
                  {signingOut ? "Signing out…" : "Sign out"}
                </Button>
              </div>
            </div>

            <div className="ml-account-part">
              <button
                ref={deleteRef}
                type="button"
                className="ml-hc-link ml-account-delete"
                onClick={() => {
                  setProblem(null);
                  setConfirming(true);
                }}
              >
                Delete my account
              </button>
            </div>

            {/* The public pages (ADR 0045 step 8), opened beside the sheet. */}
            <p className="ml-mem-quiet ml-account-pages">
              <a href={PAGE_PATHS.terms} target="_blank" rel="noopener">Terms</a>
              {" · "}
              <a href={PAGE_PATHS.privacy} target="_blank" rel="noopener">Privacy</a>
              {" · "}
              <a href={PAGE_PATHS.refunds} target="_blank" rel="noopener">Refunds</a>
              {" · "}
              <a href={PAGE_PATHS.contact} target="_blank" rel="noopener">Contact</a>
            </p>
          </div>
        )}
      </div>
    </div>,
    document.body,
  );
}
