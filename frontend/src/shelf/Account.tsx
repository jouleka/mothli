/** Hosted mode only: the reader's account, opened from the shelf's settings menu. Who is signed
 * in, "What Mothli remembers" when the server keeps recollections (not shown where it answers
 * 404), signing out, which also clears the books saved for offline reading on this device, and,
 * quietly at the end, deleting the account: a second step says plainly what happens and needs a
 * deliberate yes. A modal sheet like "What Mothli remembers" (fl-scrim / fl-sheet), with the same
 * focus handling. Taking a copy of one's data is not here yet. */
import { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api, ApiError } from "../api";
import type { OfflineSession } from "../pwa/offline";
import { keepTabFocusInside } from "../reader/focusTrap";
import { Button } from "../ui/Button";
import { Icon } from "../ui/Icon";
import { MothliRemembers } from "./MothliRemembers";
import { noteAccountDeleted } from "./SignIn";
import "../library/free-library.css";
import "./home-chat.css";
import "./account.css";

const SIGN_OUT_FAILED = "I couldn’t finish signing you out. Check your connection and try again.";
const DELETE_FAILED = "I couldn’t do that just now. Check your connection and try again.";

export function Account({ session, onClose }: { session: OfflineSession; onClose: () => void }) {
  // null until the server answers; false where it keeps no recollections (404).
  const [remembers, setRemembers] = useState<boolean | null>(null);
  const [remembering, setRemembering] = useState(false);
  const [signingOut, setSigningOut] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  // Deleting: the second step, the reader's "I understand", and the request on its way.
  const [confirming, setConfirming] = useState(false);
  const [understood, setUnderstood] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const rememberRef = useRef<HTMLButtonElement>(null);
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

  // A modal: the page behind is inert, Escape closes it, Tab stays inside. While "What Mothli
  // remembers" is open on top, that sheet owns the keyboard instead.
  useEffect(() => {
    if (remembering) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const siblings = Array.from(document.body.children)
      .filter((element) => element !== rootRef.current)
      .map((element) => ({ element: element as HTMLElement, inert: (element as HTMLElement).inert }));
    for (const { element } of siblings) element.inert = true;
    if (returnToRemember.current) {
      returnToRemember.current = false;
      rememberRef.current?.focus();
    } else closeRef.current?.focus();
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
  }, [remembering]);

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
          </div>
        )}
      </div>
    </div>,
    document.body,
  );
}
