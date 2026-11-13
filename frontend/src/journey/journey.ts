/** The reading journey: daily streaks, milestones, and which cast members a reader has already
 * "unlocked". Client-side and per device (localStorage); every read and write degrades silently
 * where storage is unavailable, so the journey is a delight layer and never a dependency. */

const DAYS_KEY = "mothli.journey.days";
const CAST_KEY = (bookId: string) => `mothli.journey.cast:${bookId}`;

function dayKey(date = new Date()) {
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, "0");
  const d = String(date.getDate()).padStart(2, "0");
  return `${y}-${m}-${d}`;
}

function readDays(): string[] {
  try {
    const value = JSON.parse(localStorage.getItem(DAYS_KEY) ?? "[]");
    return Array.isArray(value) ? value.filter((item) => typeof item === "string") : [];
  } catch {
    return [];
  }
}

/** Mark today as a reading day. Returns the streak including today. */
export function recordReadingDay(now = new Date()): number {
  const days = readDays();
  const today = dayKey(now);
  if (!days.includes(today)) {
    days.push(today);
    try {
      localStorage.setItem(DAYS_KEY, JSON.stringify(days.slice(-400)));
    } catch {
      /* storage unavailable */
    }
  }
  return readingStreak(now);
}

/** Consecutive reading days ending today (or yesterday, so a streak survives until bedtime). */
export function readingStreak(now = new Date()): number {
  const days = new Set(readDays());
  const cursor = new Date(now);
  if (!days.has(dayKey(cursor))) cursor.setDate(cursor.getDate() - 1);
  let streak = 0;
  while (days.has(dayKey(cursor))) {
    streak += 1;
    cursor.setDate(cursor.getDate() - 1);
  }
  return streak;
}

/** The last seven days, oldest first, for a small streak strip. */
export function recentDays(now = new Date()): { key: string; label: string; read: boolean; today: boolean }[] {
  const days = new Set(readDays());
  return Array.from({ length: 7 }, (_, index) => {
    const date = new Date(now);
    date.setDate(date.getDate() - (6 - index));
    const key = dayKey(date);
    return {
      key,
      label: date.toLocaleDateString(undefined, { weekday: "narrow" }),
      read: days.has(key),
      today: index === 6,
    };
  });
}

const MILESTONES = [0.25, 0.5, 0.75, 1] as const;
/** The milestone crossed by moving from `before` to `after` completed chapters, if any. */
export function crossedMilestone(before: number, after: number, total: number): number | null {
  if (total < 4 || after <= before) return null;
  for (const mark of [...MILESTONES].reverse()) {
    const threshold = Math.ceil(total * mark);
    if (before < threshold && after >= threshold) return mark;
  }
  return null;
}

export function milestoneLine(mark: number, title: string) {
  if (mark >= 1) return `You finished ${title}. What a journey.`;
  if (mark >= 0.75) return `Three quarters of the way through ${title}. The ending is getting close.`;
  if (mark >= 0.5) return `Halfway through ${title}!`;
  return `A quarter of the way through ${title}. You’re properly in it now.`;
}

export function unlockedCast(bookId: string): Set<number> {
  try {
    const value = JSON.parse(localStorage.getItem(CAST_KEY(bookId)) ?? "[]");
    return new Set(Array.isArray(value) ? value.filter((item) => typeof item === "number") : []);
  } catch {
    return new Set();
  }
}

/** Remember cast members as unlocked; returns the ids that were new. */
export function unlockCast(bookId: string, ids: number[]): number[] {
  const known = unlockedCast(bookId);
  const fresh = ids.filter((id) => !known.has(id));
  if (!fresh.length) return [];
  fresh.forEach((id) => known.add(id));
  try {
    localStorage.setItem(CAST_KEY(bookId), JSON.stringify([...known]));
  } catch {
    /* storage unavailable */
  }
  return fresh;
}
