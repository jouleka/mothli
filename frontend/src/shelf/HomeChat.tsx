/** Mothli on the home screen: one conversation across the reader's books, written the way Mothli
 * writes in a book's margin (the reader in graphite, Mothli in blue pencil). Every sentence shown
 * has already been checked on the server; this panel only displays what arrives. It is not
 * rendered at all where the server has no home conversation.
 *
 * Long conversations stay on one sheet: the conversation scrolls inside a bounded area that keeps
 * the newest exchange in view, and only the latest exchanges are drawn until the reader asks for
 * earlier ones. */
import { type FormEvent, useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { api, ApiError, type HomeTurn, type ReadSkipped, type SuggestedBook } from "../api";
import { Button } from "../ui/Button";
import { companionPause, needsPlan } from "../ui/companionPause";
import { Icon } from "../ui/Icon";
import { Errand } from "../ui/Errand";
import { useCycle } from "../ui/useCycle";
import { Moth } from "../ui/Moth";
import { HOME_HELLO_NO_BOOKS, MOTHLI_HELLO, MothliEndingNote, MothliInvitation } from "../ui/MothliPlan";
import { noteMothliUsed, useMothliStanding } from "../ui/mothliStanding";
import { MothliRemembers } from "./MothliRemembers";
import "./home-chat.css";

const NOTHING_SHOWN = "We haven’t got there yet, or I don’t have enough from what we’ve read to say.";
const STARTERS = ["Where did I leave off?", "What should I read tonight?"];
const INTRO = "Ask me about any book on your shelf: where you left off, who someone was, what to read next.";
/** For a reader with nothing on the shelf yet, where "Where did I leave off?" means nothing. */
const FIRST_STARTERS = ["What can you do?", "What should I read first?"];
const FIRST_INTRO = "Nothing on your shelf yet? Ask me what I can do, or for a good first book to start with.";
/** How many exchanges are drawn at first, and how many more each "Show earlier" adds. */
const LATEST = 6;
const EARLIER_STEP = 10;
/** The writer can take a while before its first sentence; after this long, say so. */
const SLOW_MS = 6000;
/** Within this many pixels of the bottom, the reader is "following" the conversation. */
const FOLLOW_SLACK = 48;

type Exchange = { question: string | null; reply: string | null };
/** Mothli's offer to count chapters it missed as read. Offers are not saved with the conversation:
 * they live only on the reply that just arrived, for this visit. */
type Offer = ReadSkipped & { state: "open" | "saving" | "done" | "failed" };

const offerLabel = (offer: ReadSkipped) => `I’ve read ${offer.chapters} of ${offer.title}`;
const offerDone = (offer: ReadSkipped) => `Got it. I’ll count ${offer.chapters} of ${offer.title} as read.`;
const OFFER_FAILED = "I couldn’t note that just now. Try again in a moment.";
/** A book Mothli suggested from the free library, with a way to put it on the shelf. Like offers,
 * these live only on the reply that just arrived. Which books a reply named is decided on the
 * server from the sentences that were shown. */
type Suggestion = SuggestedBook & { state: "open" | "saving" | "done" | "failed" };

const addLabel = (book: SuggestedBook) => `Add ${book.title} to my shelf`;
const addDone = (book: SuggestedBook) => `${book.title} is on your shelf now.`;
const ADD_FAILED = "I couldn’t add that just now. Try again in a moment.";
/** What Mothli is up to while a reply is on its way, one after another. */
export const THINKING_LINES = [
  "Thinking it over…",
  "Flipping back through your shelf…",
  "Checking my notes in the margin…",
  "Got it. Writing it down…",
];

function exchangesOf(turns: HomeTurn[]): Exchange[] {
  const out: Exchange[] = [];
  for (const turn of turns) {
    const last = out[out.length - 1];
    if (turn.role === "user") out.push({ question: turn.content, reply: null });
    else if (last && last.question !== null && last.reply === null) last.reply = turn.content;
    else out.push({ question: null, reply: turn.content });
  }
  return out;
}

export function HomeChat({ onShelfChanged, shelfEmpty = false }: {
  onShelfChanged?: () => void;
  /** Nothing on the shelf: the first question and the line above it fit a new reader. */
  shelfEmpty?: boolean;
} = {}) {
  const [turns, setTurns] = useState<HomeTurn[] | null>(null); // null until the server says it exists
  // Where the reader stands with Mothli (ADR 0045): after the free weeks, without the plan, the
  // invitation stands in place of the conversation; a new reader is greeted with the free weeks.
  const standing = useMothliStanding();
  const ended = standing?.state === "ended";
  const wasEnded = useRef(ended);
  // Whether the conversation was ever on screen here, so the invitation replacing it is read out.
  const showedChat = useRef(false);
  if (!ended && turns !== null) showedChat.current = true;
  const [start, setStart] = useState(0); // first exchange drawn; earlier ones wait behind "Show earlier"
  const [question, setQuestion] = useState("");
  const [asked, setAsked] = useState<string | null>(null); // the question in flight
  const [sentences, setSentences] = useState<string[]>([]); // the reply in flight, as it arrives
  const [slow, setSlow] = useState(false);
  const [spoken, setSpoken] = useState<string[]>([]); // what the screen reader is told, once each
  const [error, setError] = useState<string | null>(null);
  // Mothli stopping for a while (the day's talk used up, or still on the last question): said
  // calmly, not as an error.
  const [pause, setPause] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [unseen, setUnseen] = useState(false); // something newer arrived below where the reader is
  const [offers, setOffers] = useState<{ exchange: number; items: Offer[] } | null>(null);
  const [suggested, setSuggested] = useState<{ exchange: number; items: Suggestion[] } | null>(null);
  const titleId = useId();
  const inputId = useId();
  const confirmId = useId();
  const threadRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const clearRef = useRef<HTMLButtonElement>(null);
  const keepRef = useRef<HTMLButtonElement>(null);
  const following = useRef(true);
  const reveal = useRef<{ height: number; focus: number } | null>(null);
  const placed = useRef(false);
  // "What Mothli remembers": open, and whether focus is owed back to the button that opened it.
  const [remembering, setRemembering] = useState(false);
  const rememberRef = useRef<HTMLButtonElement>(null);
  const returnFocus = useRef(false);

  useEffect(() => {
    if (remembering || !returnFocus.current) return;
    returnFocus.current = false;
    rememberRef.current?.focus();
  }, [remembering]);

  // Leaving the home screen while a reply is written drops it: stop the request too, so the server
  // does not store a reply the reader never saw. (Nothing else stops a reply here: a new message
  // and "Clear conversation" both wait until it is done.)
  const request = useRef<AbortController | null>(null);
  useEffect(() => () => request.current?.abort(), []);

  useEffect(() => {
    // Ended: nothing of the conversation is asked for or kept on screen. Joined again: it comes
    // back from the server, through the same filter as always.
    if (ended) {
      setTurns([]);
      setStart(0);
      setOffers(null);
      setSuggested(null);
      return;
    }
    let dead = false;
    api.homeConversation()
      .then((saved) => {
        if (dead) return;
        setTurns(saved.turns);
        setStart(Math.max(0, exchangesOf(saved.turns).length - LATEST));
      })
      .catch(() => { /* 404: no home conversation here; the panel stays hidden */ });
    return () => { dead = true; };
  }, [ended]);

  // Back from the invitation (the reader joined): the box takes focus if the invitation had it.
  useEffect(() => {
    const active = document.activeElement;
    if (wasEnded.current && !ended && (!active || active === document.body)) inputRef.current?.focus();
    wasEnded.current = ended;
  }, [ended]);

  const busy = asked !== null;
  const waiting = busy && sentences.length === 0;
  const thinkingLine = useCycle(THINKING_LINES, 2600, waiting);

  useEffect(() => {
    if (!waiting) return;
    setSlow(false);
    const timer = window.setTimeout(() => setSlow(true), SLOW_MS);
    return () => window.clearTimeout(timer);
  }, [waiting]);

  // Once the reply has been spoken, take it out of the hidden log so a screen reader browsing the
  // page does not meet it twice.
  useEffect(() => {
    if (busy || spoken.length === 0) return;
    const timer = window.setTimeout(() => setSpoken([]), 5000);
    return () => window.clearTimeout(timer);
  }, [busy, spoken.length]);

  // First sight of a saved conversation: open at the top of the latest exchange.
  useLayoutEffect(() => {
    const thread = threadRef.current;
    if (placed.current || !thread) return;
    placed.current = true;
    const latest = thread.querySelector<HTMLElement>("[data-latest]");
    thread.scrollTop = latest ? latest.offsetTop : thread.scrollHeight;
  }, [turns]);

  // While a reply is written, keep its newest sentence in view, unless the reader scrolled back.
  useLayoutEffect(() => {
    const thread = threadRef.current;
    if (!thread || !placed.current) return;
    if (following.current) thread.scrollTop = thread.scrollHeight;
    else if (busy || sentences.length) setUnseen(true);
  }, [asked, sentences.length, turns?.length]);

  // "Show earlier": keep what the reader was looking at still, then hand focus to the nearest
  // earlier exchange so reading carries on upwards from there.
  useLayoutEffect(() => {
    const thread = threadRef.current;
    const pending = reveal.current;
    if (!thread || !pending) return;
    reveal.current = null;
    thread.scrollTop += thread.scrollHeight - pending.height;
    thread.querySelector<HTMLElement>(`[data-exchange="${pending.focus}"]`)?.focus();
  }, [start]);

  if (turns === null && !ended) return null;
  const exchanges = exchangesOf(turns ?? []);
  const empty = exchanges.length === 0 && !busy;

  const toBottom = () => {
    const thread = threadRef.current;
    following.current = true;
    setUnseen(false);
    if (!thread) return;
    thread.scrollTop = thread.scrollHeight;
    // The button goes away; keyboard focus lands on the newest exchange rather than the page.
    thread.querySelector<HTMLElement>("[data-latest]")?.focus({ preventScroll: true });
  };

  const send = async (value: string, fromBox: boolean) => {
    if (value.length < 2 || busy) return;
    setError(null);
    setPause(null);
    setConfirming(false);
    setAsked(value);
    setSentences([]);
    setSpoken(["Mothli is thinking it over."]);
    if (fromBox) setQuestion("");
    following.current = true;
    setUnseen(false);
    const streamed: string[] = [];
    const controller = new AbortController();
    request.current = controller;
    try {
      const answer = await api.askHome(value, (sentence) => {
        streamed.push(sentence);
        setSentences((current) => [...current, sentence]);
        setSpoken((current) => [...current, sentence]);
      }, controller.signal);
      noteMothliUsed();
      const claims = answer.claims.map((claim) => claim.text);
      const reply = claims.join(" ") || NOTHING_SHOWN;
      const unsaid = claims.length ? claims.filter((text) => !streamed.includes(text)) : [NOTHING_SHOWN];
      if (unsaid.length) setSpoken((current) => [...current, ...unsaid]);
      const next: HomeTurn[] = [...turns ?? [], { role: "user", content: value }, { role: "assistant", content: reply }];
      setTurns(next); // nothing else changes the conversation while a reply is on its way
      if (answer.offers?.length) {
        setOffers({
          exchange: exchangesOf(next).length - 1,
          items: answer.offers.slice(0, 3).map((offer) => ({ ...offer, state: "open" })),
        });
      }
      if (answer.suggested?.length) {
        setSuggested({
          exchange: exchangesOf(next).length - 1,
          items: answer.suggested.slice(0, 3).map((book) => ({ ...book, state: "open" })),
        });
      }
    } catch (reason) {
      setQuestion((current) => current || value);
      setSpoken([]);
      // Mothli needs the plan: the invitation takes the conversation's place, and no line is said.
      if (needsPlan(reason)) return;
      const paused = companionPause(reason, { shelf: true });
      if (paused) setPause(paused.line);
      else setError(reason instanceof ApiError && reason.status === 503
        ? "Mothli can’t check its reply right now, so it said nothing. Try again in a moment."
        : "That didn’t go through. Your question is still here.");
    } finally {
      if (request.current === controller) request.current = null;
      setAsked(null);
      setSentences([]);
    }
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    void send(question.trim(), true);
  };

  const accept = async (index: number) => {
    const offer = offers?.items[index];
    if (!offer || offer.state === "saving" || offer.state === "done") return;
    const mark = (state: Offer["state"]) =>
      setOffers((current) => current && {
        ...current,
        items: current.items.map((item, at) => (at === index ? { ...item, state } : item)),
      });
    mark("saving");
    try {
      await api.readChapters(offer.book_id, offer.atoms, true);
      mark("done");
      setSpoken((current) => [...current, offerDone(offer)]);
    } catch {
      mark("failed");
      setSpoken((current) => [...current, OFFER_FAILED]);
    }
  };

  const add = async (index: number) => {
    const book = suggested?.items[index];
    if (!book || book.state === "saving" || book.state === "done") return;
    const mark = (state: Suggestion["state"]) =>
      setSuggested((current) => current && {
        ...current,
        items: current.items.map((item, at) => (at === index ? { ...item, state } : item)),
      });
    mark("saving");
    try {
      await api.addClassic(book.classic_id);
    } catch (reason) {
      // 409: it is on the shelf already (added from the library meanwhile), which is what was asked.
      if (!(reason instanceof ApiError && reason.status === 409)) {
        mark("failed");
        setSpoken((current) => [...current, ADD_FAILED]);
        return;
      }
    }
    mark("done");
    setSpoken((current) => [...current, addDone(book)]);
    onShelfChanged?.();
  };

  const clear = async () => {
    setTurns([]);
    setOffers(null);
    setSuggested(null);
    setStart(0);
    setError(null);
    setPause(null);
    setConfirming(false);
    setUnseen(false);
    placed.current = false;
    inputRef.current?.focus();
    try {
      await api.clearHome();
    } catch {
      /* The panel is cleared; the server copy is cleared the next time this succeeds. */
    }
  };

  const showEarlier = () => {
    const thread = threadRef.current;
    reveal.current = { height: thread?.scrollHeight ?? 0, focus: start - 1 };
    setStart(Math.max(0, start - EARLIER_STEP));
  };

  return (
    <section className="ml-home-chat" aria-labelledby={titleId} aria-busy={busy || undefined}>
      <header className="ml-hc-head">
        <Moth size={46} mood={busy ? "thinking" : "idle"} className="ml-hc-moth" />
        <div className="ml-hc-title">
          <h2 id={titleId}>Talk with Mothli about your books</h2>
          <p>One conversation across your whole shelf.</p>
        </div>
        <div className="ml-hc-actions">
          <button
            ref={rememberRef}
            type="button"
            className="ml-hc-link ml-hc-clear"
            aria-haspopup="dialog"
            onClick={() => setRemembering(true)}
          >
            What Mothli remembers
          </button>
          {exchanges.length > 0 && !confirming && !ended && (
            <button
              ref={clearRef}
              type="button"
              className="ml-hc-link ml-hc-clear"
              disabled={busy}
              onClick={() => {
                setConfirming(true);
                window.setTimeout(() => keepRef.current?.focus());
              }}
            >
              Clear conversation
            </button>
          )}
        </div>
      </header>

      {remembering && (
        <MothliRemembers
          onClose={() => {
            returnFocus.current = true;
            setRemembering(false);
          }}
        />
      )}

      {confirming && (
        <div className="ml-hc-confirm" role="group" aria-labelledby={confirmId}>
          <span id={confirmId}>Clear the whole conversation? Mothli forgets it too.</span>
          <button type="button" className="ml-hc-link" onClick={() => void clear()}>Clear it</button>
          <button
            ref={keepRef}
            type="button"
            className="ml-hc-link ml-hc-keep"
            onClick={() => {
              setConfirming(false);
              window.setTimeout(() => clearRef.current?.focus());
            }}
          >
            Keep it
          </button>
        </div>
      )}

      {ended ? (
        <MothliInvitation why={standing?.why ?? null} home replaced={showedChat.current} />
      ) : (
      <>
      <MothliEndingNote home after={inputRef} />
      {empty ? (
        <div className="ml-hc-hello">
          {standing?.state === "new" && (
            <p className="ml-hc-hand">{shelfEmpty ? HOME_HELLO_NO_BOOKS : MOTHLI_HELLO}</p>
          )}
          <p className="ml-hc-hand">{shelfEmpty ? FIRST_INTRO : INTRO}</p>
          <div className="ml-hc-starters">
            {(shelfEmpty ? FIRST_STARTERS : STARTERS).map((starter) => (
              <button key={starter} type="button" className="ml-hc-starter" onClick={() => void send(starter, false)}>
                {starter}
              </button>
            ))}
          </div>
        </div>
      ) : (
        <div
          className="ml-hc-thread"
          ref={threadRef}
          role="region"
          aria-label="Conversation with Mothli"
          tabIndex={0}
          onScroll={(event) => {
            const node = event.currentTarget;
            following.current = node.scrollHeight - node.scrollTop - node.clientHeight < FOLLOW_SLACK;
            if (following.current) setUnseen(false);
          }}
        >
          {start > 0 && (
            <button type="button" className="ml-hc-link ml-hc-earlier" onClick={showEarlier}>
              Show earlier ({start} more)
            </button>
          )}
          <ol className="ml-hc-turns">
            {exchanges.slice(start).map((exchange, offset) => {
              const index = start + offset;
              return (
                <li
                  key={index}
                  className="ml-hc-exchange"
                  data-exchange={index}
                  data-latest={(!busy && index === exchanges.length - 1) || undefined}
                  tabIndex={-1}
                >
                  {exchange.question !== null && (
                    <p className="ml-hc-you"><span className="sr-only">You: </span>{exchange.question}</p>
                  )}
                  {exchange.reply !== null && (
                    <p className="ml-hc-reply"><span className="sr-only">Mothli: </span>{exchange.reply}</p>
                  )}
                  {offers?.exchange === index && (
                    <ul className="ml-hc-offers">
                      {offers.items.map((offer, at) => (
                        <li key={`${offer.book_id}-${at}`}>
                          {offer.state === "done" ? (
                            <p className="ml-hc-offer-done">{offerDone(offer)}</p>
                          ) : (
                            <>
                              <button
                                type="button"
                                className="ml-hc-offer"
                                disabled={offer.state === "saving"}
                                aria-busy={offer.state === "saving" || undefined}
                                onClick={() => void accept(at)}
                              >
                                <Icon name="check" size={15} />
                                {offerLabel(offer)}
                              </button>
                              {offer.state === "failed" && <p className="ml-hc-offer-failed">{OFFER_FAILED}</p>}
                            </>
                          )}
                        </li>
                      ))}
                    </ul>
                  )}
                  {suggested?.exchange === index && (
                    <ul className="ml-hc-offers">
                      {suggested.items.map((book, at) => (
                        <li key={book.classic_id}>
                          {book.state === "done" ? (
                            <p className="ml-hc-offer-done ml-hc-offer-done--fresh">{addDone(book)}</p>
                          ) : book.state === "saving" ? (
                            <Errand kind="library" title={book.title} className="ml-hc-errand" />
                          ) : (
                            <>
                              <button
                                type="button"
                                className="ml-hc-offer"
                                onClick={() => void add(at)}
                              >
                                <Icon name="plus" size={15} />
                                {addLabel(book)}
                              </button>
                              {book.state === "failed" && <p className="ml-hc-offer-failed">{ADD_FAILED}</p>}
                            </>
                          )}
                        </li>
                      ))}
                    </ul>
                  )}
                </li>
              );
            })}
            {busy && (
              <li className="ml-hc-exchange" data-latest tabIndex={-1}>
                <p className="ml-hc-you"><span className="sr-only">You: </span>{asked}</p>
                {sentences.length > 0 && (
                  <p className="ml-hc-reply">
                    <span className="sr-only">Mothli: </span>
                    {sentences.map((sentence, index) => (
                      <span key={index} className="ml-hc-sentence">{index > 0 ? " " : ""}{sentence}</span>
                    ))}
                  </p>
                )}
                <p className="ml-hc-thinking">
                  <span className="ml-hc-dots" aria-hidden="true"><i /><i /><i /></span>
                  {waiting && <span key={slow ? "slow" : thinkingLine}>{slow ? "Still thinking. Careful replies take a little while." : thinkingLine}</span>}
                </p>
              </li>
            )}
          </ol>
          {unseen && (
            <button type="button" className="ml-hc-jump" onClick={toBottom}>
              <Icon name="arrowRight" size={14} />
              Jump to the newest reply
            </button>
          )}
        </div>
      )}

      <div className="sr-only" role="log" aria-label="Mothli’s reply as it arrives">
        {spoken.map((line, index) => <p key={index}>{line}</p>)}
      </div>

      {error && <p role="alert" className="ml-hc-error">{error}</p>}
      <p role="status" className="ml-hc-pause" hidden={!pause}>{pause}</p>

      <form className="ml-hc-composer" onSubmit={submit}>
        <label className="sr-only" htmlFor={inputId}>Ask Mothli about your books</label>
        <textarea
          id={inputId}
          ref={inputRef}
          rows={1}
          maxLength={500}
          value={question}
          placeholder={empty ? "Ask Mothli anything…" : "Ask a follow-up…"}
          onChange={(event) => setQuestion(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) submit(event);
          }}
        />
        <Button
          variant="primary"
          type="submit"
          className="ml-hc-send"
          aria-label="Ask"
          title="Ask"
          disabled={busy || question.trim().length < 2}
        >
          <Icon name="arrowRight" size={18} />
        </Button>
      </form>
      <p className="ml-hc-fine">
        <Icon name="shield" size={12} />
        Only the pages you’ve read. No spoilers.
      </p>
      </>
      )}
    </section>
  );
}
