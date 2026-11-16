/** "Tell me" after a chapter is finished. Mothli's notes on a chapter are taken in the background
 * once it is finished, so the closeout can answer 409 ("not remembered yet") for a little while.
 * That is a short wait, not an answer: ask again a few times, quietly, before telling the reader
 * to come back. Anything other than 409 is answered at once. */
import { ApiError } from "../api";

export const CLOSEOUT_WAITS = [3_000, 5_000, 8_000, 12_000]; // about half a minute in all

export async function untilNotesReady<T>(
  load: () => Promise<T>,
  keepGoing: () => boolean,
  waits: number[] = CLOSEOUT_WAITS,
  sleep: (ms: number) => Promise<void> = (ms) => new Promise((done) => setTimeout(done, ms)),
): Promise<T> {
  for (let attempt = 0; ; attempt++) {
    try {
      return await load();
    } catch (reason) {
      if (!(reason instanceof ApiError && reason.status === 409) || attempt >= waits.length || !keepGoing()) throw reason;
    }
    await sleep(waits[attempt]);
    if (!keepGoing()) throw new ApiError("stopped waiting", 409);
  }
}
