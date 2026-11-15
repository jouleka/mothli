/** The cast and recap show what has been extracted, up to the chapter counter. When the counter
 * moves without a page turn ("I've read chapters 1 and 2"), the extraction behind it takes a
 * while; this watches it and reports each chapter as it lands, so those views refresh on their
 * own. It stops once extraction reaches the counter, stops running, fails, or the reader leaves. */
import { api, type IngestStatus } from "../api";

type Options = { fetch?: (bookId: string) => Promise<IngestStatus>; intervalMs?: number };

export function watchMemory(
  bookId: string,
  target: number,
  onProgress: (progress: number) => void,
  { fetch = api.ingest, intervalMs = 4000 }: Options = {},
): () => void {
  let stopped = target < 1;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let last = -1;
  const check = async () => {
    if (stopped) return;
    let state: IngestStatus;
    try {
      state = await fetch(bookId);
    } catch {
      return;
    }
    if (stopped) return;
    if (state.ingest_progress !== last) {
      last = state.ingest_progress;
      onProgress(last);
    }
    if (state.ingest_progress < target && state.status === "running") timer = setTimeout(check, intervalMs);
  };
  if (!stopped) timer = setTimeout(check, 0);
  return () => {
    stopped = true;
    if (timer) clearTimeout(timer);
  };
}
