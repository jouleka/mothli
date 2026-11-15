/** Under a reply in which Mothli said it was not with the reader for some chapters: one press
 * tells Mothli they have been read. The server decides when to offer this; it can be undone
 * from the contents list, so there is no confirmation step. */
import { useState } from "react";
import { api, type ReadSkipped } from "../api";

export function CountAsRead({ offers, onCounted }: { offers: ReadSkipped[]; onCounted?: () => void }) {
  const [state, setState] = useState<Record<string, "busy" | "done" | "failed">>({});
  const count = async (offer: ReadSkipped) => {
    setState((current) => ({ ...current, [offer.book_id]: "busy" }));
    try {
      await api.readChapters(offer.book_id, offer.atoms, true);
      setState((current) => ({ ...current, [offer.book_id]: "done" }));
      onCounted?.();
    } catch {
      setState((current) => ({ ...current, [offer.book_id]: "failed" }));
    }
  };
  return (
    <div className="mothli-count-read">
      {offers.map((offer) =>
        state[offer.book_id] === "done" ? (
          <p key={offer.book_id} role="status">Got it. I’ll count {offer.chapters} as read.</p>
        ) : (
          <p key={offer.book_id}>
            <button
              type="button"
              className="plain mothli-count-read-button"
              disabled={state[offer.book_id] === "busy"}
              onClick={() => void count(offer)}
            >
              I’ve read {offer.chapters}
            </button>
            {state[offer.book_id] === "failed" && (
              <span role="alert"> That didn’t go through. Try once more.</span>
            )}
          </p>
        ),
      )}
    </div>
  );
}
