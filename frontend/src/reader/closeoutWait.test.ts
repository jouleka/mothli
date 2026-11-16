import { describe, expect, it } from "vitest";
import { ApiError } from "../api";
import { untilNotesReady } from "./closeoutWait";

const notYet = () => new ApiError("chapter is not completed and remembered yet", 409);

describe("Tell me, right after a chapter is finished", () => {
  // The break: the first 409 ("notes still being taken") shown at once as "Ask me again in a
  // minute" with a Retry button, for a chapter whose notes arrive a few seconds later.
  it("waits quietly while the chapter's notes are being taken", async () => {
    const answers: Array<() => Promise<string>> = [
      () => Promise.reject(notYet()), () => Promise.reject(notYet()), () => Promise.resolve("noticed"),
    ];
    const slept: number[] = [];
    const result = await untilNotesReady(() => answers.shift()!(), () => true, [10, 20, 30],
      async (ms) => { slept.push(ms); });
    expect(result).toBe("noticed");
    expect(slept).toEqual([10, 20]);
  });

  it("stops after a while, and at once for any other answer or when the reader has moved on", async () => {
    const slept: number[] = [];
    await expect(untilNotesReady(() => Promise.reject(notYet()), () => true, [10, 20],
      async (ms) => { slept.push(ms); })).rejects.toMatchObject({ status: 409 });
    expect(slept).toEqual([10, 20]);
    let calls = 0;
    await expect(untilNotesReady(() => { calls++; return Promise.reject(new ApiError("limit", 429)); }, () => true,
      [10], async () => {})).rejects.toMatchObject({ status: 429 });
    expect(calls).toBe(1);
    calls = 0;
    await expect(untilNotesReady(() => { calls++; return Promise.reject(notYet()); }, () => false,
      [10, 20], async () => {})).rejects.toMatchObject({ status: 409 });
    expect(calls).toBe(1);
  });
});
