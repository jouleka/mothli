/** Mothli on the home screen: one conversation across the reader's books. Every sentence shown
 * has already been checked on the server; this panel only displays what arrives. It is not
 * rendered at all where the server has no home conversation. */
import { type FormEvent, useEffect, useId, useState } from "react";
import { api, ApiError, type HomeTurn } from "../api";
import { Button } from "../ui/Button";

const NOTHING_SHOWN = "We haven’t got there yet, or I don’t have enough from what we’ve read to say.";

export function HomeChat() {
  const [turns, setTurns] = useState<HomeTurn[] | null>(null); // null until the server says it exists
  const [question, setQuestion] = useState("");
  const [arriving, setArriving] = useState<string | null>(null); // sentences of the reply in flight
  const [asked, setAsked] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const titleId = useId();
  const inputId = useId();

  useEffect(() => {
    let dead = false;
    api.homeConversation()
      .then((saved) => { if (!dead) setTurns(saved.turns); })
      .catch(() => { /* 404: no home conversation here; the panel stays hidden */ });
    return () => { dead = true; };
  }, []);

  if (turns === null) return null;
  const busy = asked !== null;

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const value = question.trim();
    if (value.length < 2 || busy) return;
    setError(null);
    setAsked(value);
    setArriving("");
    try {
      const answer = await api.askHome(value, (sentence) =>
        setArriving((current) => [current, sentence].filter(Boolean).join(" ")));
      const reply = answer.claims.map((claim) => claim.text).join(" ") || NOTHING_SHOWN;
      setTurns((current) => [...(current ?? []), { role: "user", content: value }, { role: "assistant", content: reply }]);
      setQuestion("");
    } catch (reason) {
      setError(reason instanceof ApiError && reason.status === 503
        ? "Mothli can’t check its reply right now, so it said nothing. Try again in a moment."
        : "That didn’t go through. Your question is still here.");
    } finally {
      setAsked(null);
      setArriving(null);
    }
  };

  const clear = async () => {
    setTurns([]);
    setError(null);
    try {
      await api.clearHome();
    } catch {
      /* The panel is cleared; the server copy is cleared the next time this succeeds. */
    }
  };

  return (
    <section className="ml-home-chat" aria-labelledby={titleId}>
      <div className="ml-home-chat-head">
        <h2 id={titleId}>Talk with Mothli about your books</h2>
        {turns.length > 0 && (
          <button type="button" className="plain ml-home-chat-clear" onClick={() => void clear()}>
            Clear conversation
          </button>
        )}
      </div>
      {(turns.length > 0 || busy) && (
        <ol className="ml-home-chat-turns" aria-live="polite">
          {turns.map((turn, index) => (
            <li key={index} className={turn.role === "user" ? "is-reader" : "is-mothli"}>
              <span className="sr-only">{turn.role === "user" ? "You: " : "Mothli: "}</span>
              {turn.content}
            </li>
          ))}
          {asked !== null && <li className="is-reader"><span className="sr-only">You: </span>{asked}</li>}
          {asked !== null && (
            <li className="is-mothli">
              <span className="sr-only">Mothli: </span>
              {arriving || "…"}
            </li>
          )}
        </ol>
      )}
      {error && <p role="alert" className="ml-home-chat-error">{error}</p>}
      <form className="ml-home-chat-form" onSubmit={(event) => void submit(event)}>
        <label className="sr-only" htmlFor={inputId}>Ask Mothli about your books</label>
        <textarea
          id={inputId}
          rows={2}
          maxLength={500}
          value={question}
          placeholder="Where did I leave off? What should I pick up tonight?"
          onChange={(event) => setQuestion(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) void submit(event);
          }}
        />
        <Button variant="primary" type="submit" disabled={busy || question.trim().length < 2}>
          Ask
        </Button>
      </form>
    </section>
  );
}
