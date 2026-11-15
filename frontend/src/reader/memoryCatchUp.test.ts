import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import type { IngestStatus } from "../api";
import { watchMemory } from "./memoryCatchUp";

const status = (ingest_progress: number, state = "running"): IngestStatus =>
  ({ ingest_progress, status: state, flags: [], error: null });

describe("the cast and recap catch up with chapters marked read", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  test("each chapter extracted is reported as it lands, and watching stops at the counter", async () => {
    const replies = [status(0), status(1), status(1), status(2)];
    const fetch = vi.fn(async () => replies.shift() ?? status(2));
    const seen: number[] = [];
    watchMemory("b", 2, (progress) => seen.push(progress), { fetch, intervalMs: 1000 });
    await vi.advanceTimersByTimeAsync(0);
    expect(seen).toEqual([0]);
    await vi.advanceTimersByTimeAsync(3000);
    expect(seen).toEqual([0, 1, 2]);
    await vi.advanceTimersByTimeAsync(5000);
    expect(fetch).toHaveBeenCalledTimes(4);           // caught up: no more checks
  });

  test("it stops when extraction is not running, and when the reader leaves", async () => {
    const idle = vi.fn(async () => status(0, "idle"));
    watchMemory("b", 2, () => {}, { fetch: idle, intervalMs: 1000 });
    await vi.advanceTimersByTimeAsync(5000);
    expect(idle).toHaveBeenCalledTimes(1);
    const running = vi.fn(async () => status(0));
    const stop = watchMemory("b", 2, () => {}, { fetch: running, intervalMs: 1000 });
    await vi.advanceTimersByTimeAsync(1500);
    stop();
    await vi.advanceTimersByTimeAsync(5000);
    expect(running).toHaveBeenCalledTimes(2);
  });

  test("nothing is checked when there is nothing to catch up, and a failed check ends quietly", async () => {
    const fetch = vi.fn(async () => status(0));
    watchMemory("b", 0, () => {}, { fetch, intervalMs: 1000 });
    const failing = vi.fn(async () => { throw new Error("offline"); });
    watchMemory("b", 2, () => {}, { fetch: failing, intervalMs: 1000 });
    await vi.advanceTimersByTimeAsync(5000);
    expect(fetch).not.toHaveBeenCalled();
    expect(failing).toHaveBeenCalledTimes(1);
  });
});
