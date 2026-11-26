import { useEffect, useState } from "react";

/** One of `lines` at a time, moving to the next every `ms` and staying on the last, so a wait
 * reads as Mothli getting on with something instead of standing still. Starts again whenever
 * `running` turns on. */
export function useCycle(lines: readonly string[], ms: number, running = true): string {
  const [at, setAt] = useState(0);
  useEffect(() => {
    if (!running) {
      setAt(0);
      return;
    }
    if (at >= lines.length - 1) return;
    const timer = window.setTimeout(() => setAt((step) => step + 1), ms);
    return () => window.clearTimeout(timer);
  }, [at, lines.length, ms, running]);
  return lines[Math.min(at, lines.length - 1)];
}
