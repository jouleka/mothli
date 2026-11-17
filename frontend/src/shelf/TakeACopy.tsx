/** Hosted mode only: "Take a copy of my data". One quiet link that asks the server for the
 * reader's copy (one zip: app/hosted/tenant/export.py) and hands it to the browser to save, with a
 * plain line while it is made, when it is done, and when it could not be. Used in the account
 * sheet, in its delete confirmation and on the screen a reader waiting to be erased sees. */
import { useId, useRef, useState } from "react";
import { api, ApiError } from "../api";
import "./home-chat.css";
import "./account.css";

export const COPY_LABEL = "Take a copy of my data";
export const COPY_WHAT =
  "Your shelf, your place in each book, your highlights and notes, your chats and what Mothli remembers, in one file. The books themselves aren’t in it.";
export const COPY_MAKING = "Getting your copy ready…";
export const COPY_READY = "Done. Your copy is in your downloads.";
export const COPY_FAILED = "I couldn’t make your copy just now. Check your connection and try again.";

function save(blob: Blob, name: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.hidden = true;
  document.body.appendChild(link);
  link.click();
  link.remove();
  // Long enough for the browser to start saving it.
  window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
}

export function TakeACopy({ what = COPY_WHAT }: { what?: string }) {
  const [state, setState] = useState<"idle" | "making" | "ready">("idle");
  const [problem, setProblem] = useState<string | null>(null);
  const making = useRef(false);
  const whatId = useId();

  const take = async () => {
    if (making.current) return;
    making.current = true;
    setState("making");
    setProblem(null);
    try {
      const { blob, name } = await api.exportAccount();
      save(blob, name);
      setState("ready");
    } catch (reason) {
      // Too soon, or too many at once: the server's own line says so, kindly.
      setProblem(
        reason instanceof ApiError && (reason.status === 429 || reason.status === 503) && reason.code
          ? reason.message
          : COPY_FAILED,
      );
      setState("idle");
    } finally {
      making.current = false;
    }
  };

  return (
    <div className="ml-account-copy">
      <button
        type="button"
        className="ml-hc-link ml-account-remember"
        aria-describedby={whatId}
        aria-busy={state === "making" || undefined}
        disabled={state === "making"}
        onClick={() => void take()}
      >
        {COPY_LABEL}
      </button>
      <p className="ml-mem-quiet" id={whatId}>{what}</p>
      <p className="ml-mem-quiet" role="status">
        {state === "making" ? COPY_MAKING : state === "ready" ? COPY_READY : ""}
      </p>
      {problem && <p className="fl-error" role="alert">{problem}</p>}
    </div>
  );
}
