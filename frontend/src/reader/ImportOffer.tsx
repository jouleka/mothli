/** What another reading app recorded about this book, put to the reader once when they first
 * open it here. It is their word, not proof: nothing counts as read until they say yes. */
import { useId } from "react";
import type { ReadOffer } from "../api";
import { Button } from "../ui/Button";

const appName = (source: string) => source.charAt(0).toUpperCase() + source.slice(1);

function when(day: string | null): string {
  if (!day) return "";
  const date = new Date(`${day}T00:00:00`);
  return Number.isNaN(date.getTime())
    ? ""
    : ` on ${date.toLocaleDateString(undefined, { year: "numeric", month: "long", day: "numeric" })}`;
}

export function ImportOffer({
  offer,
  onAccept,
  onDecline,
}: {
  offer: ReadOffer;
  onAccept: () => void;
  onDecline: () => void;
}) {
  const titleId = useId();
  const apps = offer.sources.map(appName).join(" and ") || "Your reading history";
  const finished = offer.kind === "finished";
  const chapters = offer.atoms.length;
  if (offer.kind === "skipped") {
    const them = chapters === 1 ? "it" : "them";
    // Every chapter asked about was turned through quickly here, not leapt over.
    const quickly = chapters > 0 && offer.atoms.every((atom) => offer.skimmed?.includes(atom));
    return (
      <div className="start-over-note import-offer" role="region" aria-labelledby={titleId}>
        <div className="confirmation-copy">
          <strong id={titleId}>Before we go on</strong>
          <p>
            {quickly
              ? <>You went through {offer.chapters} quickly. Did you read {them}, or were you just getting here?</>
              : <>I wasn’t with you for {offer.chapters}. Have you read {them}?</>}
          </p>
        </div>
        <div className="confirmation-actions start-over-actions">
          <Button onClick={onDecline}>{quickly ? "I was just getting here" : `no, I skipped ${them}`}</Button>
          <Button variant="primary" onClick={onAccept}>{quickly ? `yes, I read ${them}` : `yes, I’ve read ${them}`}</Button>
        </div>
      </div>
    );
  }
  return (
    <div className="start-over-note import-offer" role="region" aria-labelledby={titleId}>
      <div className="confirmation-copy">
        <strong id={titleId}>From your reading history</strong>
        {finished ? (
          <p>
            {apps} says you finished this book{when(offer.finished_on)}. Should
            Mothli treat all of it as read?
          </p>
        ) : (
          <p>
            Your {apps} highlights reach {chapters} {chapters === 1 ? "chapter" : "chapters"} into
            this book. Should Mothli treat {chapters === 1 ? "that chapter" : "those chapters"} as read?
          </p>
        )}
      </div>
      <div className="confirmation-actions start-over-actions">
        <Button onClick={onDecline}>no, I’m starting it now</Button>
        <Button variant="primary" onClick={onAccept}>
          {finished ? "yes, I’ve read it" : "yes, I’ve read those"}
        </Button>
      </div>
    </div>
  );
}
