/** Whether the front page on screen was written out by the server before the app started
 * (main.tsx says so). The app then draws the same page over it, and the opening's drawing-in
 * carries on from where it had got to instead of starting again. */
export const arrival = { prerendered: false };

/** How long the written-out page has been on screen, roughly: since it was first painted. */
export function shownFor(): number {
  if (!arrival.prerendered || typeof performance === "undefined") return 0;
  const painted = performance.getEntriesByName?.("first-contentful-paint")[0]?.startTime ?? 0;
  return Math.max(0, performance.now() - painted);
}
