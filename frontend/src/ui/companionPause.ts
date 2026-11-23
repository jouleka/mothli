/** What the chats say when Mothli has to stop for a while in hosted mode (ADR 0041 section 6).
 * The server ends a reply with a code: ``allowance_reached`` when the reader's day or month, or
 * the whole app's day, is used up, and ``companion_busy`` while the reader's last question is
 * still being answered. Its message is already written for the reader and says which one (today,
 * this month); the lines here are only for a reply that comes without it. */
import { ApiError } from "../api";
import { MOTHLI_PLAN } from "./mothliStanding";

export const ALLOWANCE_LINE =
  "We’ve talked a lot today — let’s pick this up tomorrow. Your book and your place are right here.";
export const BUSY_LINE = "Give me a second — I’m still on your last question. Ask again in a moment.";

const finalLines = new Set([ALLOWANCE_LINE]);

const BOOK_PLACE = "Your book and your place are right here.";
const SHELF_PLACE = "Your books and your place in each are right here.";

/** The line to show, and whether asking again now could help (it cannot at the allowance).
 * `shelf`: said in the home chat, where the reader is on their shelf, so it speaks of the books
 * rather than one book (the server says it so too); only those words change. */
export function companionPause(reason: unknown, { shelf = false } = {}): { line: string; final: boolean } | null {
  if (!(reason instanceof ApiError)) return null;
  if (reason.code === "allowance_reached") {
    const said = reason.message && reason.status === 429 ? reason.message : ALLOWANCE_LINE;
    const line = shelf ? said.replace(BOOK_PLACE, SHELF_PLACE) : said;
    finalLines.add(line);
    return { line, final: true };
  }
  if (reason.code === "companion_busy") return { line: BUSY_LINE, final: false };
  return null;
}

/** Mothli needs the plan (ADR 0045): no line at all. The panel shows its invitation instead, from
 * where the reader stands (`mothliStanding`), which the answer itself has already updated. */
export function needsPlan(reason: unknown): boolean {
  return reason instanceof ApiError && reason.code === MOTHLI_PLAN;
}

/** A line shown at the allowance: no "Retry" beside it, since asking again now cannot work. */
export function isAllowanceLine(line: string): boolean {
  return finalLines.has(line);
}
