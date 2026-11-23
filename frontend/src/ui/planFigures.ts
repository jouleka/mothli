/** The plan's figures, worked out the same way wherever they are shown: the plan sheet (from
 * Paddle.js's price preview, in the reader's currency) and the front page (from Paddle's catalog,
 * read by the server). Nothing here holds a price: every amount comes in. */

function formatter(currency: string): Intl.NumberFormat | null {
  try {
    return new Intl.NumberFormat("en", { style: "currency", currency });
  } catch {
    return null;
  }
}

/** An amount in the currency's smallest unit, formatted for it ("$12.99"), or null for a
 * currency this browser does not know. */
export function formatMinor(minor: number, currency: string): string | null {
  const format = formatter(currency);
  if (!format) return null;
  const digits = format.resolvedOptions().maximumFractionDigits ?? 2;
  return format.format(minor / 10 ** digits);
}

/** The yearly plan as a month: its total over twelve, rounded to the currency's smallest unit. */
export function perMonthOf(yearlyMinor: number, currency: string): string | null {
  return formatMinor(Math.round(yearlyMinor / 12), currency);
}

/** How much less the yearly plan is than twelve months, as a whole percent rounded down; null
 * when there is no saving of at least 1%. */
export function savingOf(monthlyMinor: number, yearlyMinor: number): number | null {
  if (!(monthlyMinor > 0)) return null;
  const percent = Math.floor(((monthlyMinor * 12 - yearlyMinor) * 100) / (monthlyMinor * 12));
  return percent >= 1 ? percent : null;
}

export const yearlyMonth = (perMonth: string) => `${perMonth} a month, billed yearly`;
export const yearlyTotal = (total: string, saving: number | null) =>
  saving === null ? `${total} a year.` : `${total} a year, ${saving}% less than paying monthly.`;
export const monthlyTotal = (total: string) => `${total} a month`;
