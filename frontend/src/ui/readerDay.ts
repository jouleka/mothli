/** One way of writing a day, everywhere a reader sees one in the plan and deletion screens: in the
 * reader's own time zone, day then month, as the server's first-month line already says it
 * ("21 December"); the weekday only where the screen has one ("Thursday 24 December"); the year
 * only when it is not this one ("3 December 2027"). Put together from its parts, because browsers
 * disagree on the comma after a weekday. `timeZone` is for tests; the reader's own by default. */
export function readerDay(
  iso: string | Date,
  { weekday = false, now = new Date(), timeZone }: { weekday?: boolean; now?: Date; timeZone?: string } = {},
): string {
  const when = iso instanceof Date ? iso : new Date(iso);
  if (Number.isNaN(when.getTime())) return "";
  const parts = (moment: Date) => {
    const found: Record<string, string> = {};
    for (const part of new Intl.DateTimeFormat("en-GB", {
      weekday: "long", day: "numeric", month: "long", year: "numeric", timeZone,
    }).formatToParts(moment))
      found[part.type] = part.value;
    return found;
  };
  const day = parts(when);
  const thisYear = parts(now).year;
  return `${weekday ? `${day.weekday} ` : ""}${day.day} ${day.month}${day.year === thisYear ? "" : ` ${day.year}`}`;
}
