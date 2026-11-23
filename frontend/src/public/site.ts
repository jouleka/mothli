/** What the public pages read from the server, signed out (ADR 0045 step 8): `GET /api/site`.
 *
 * `operator`: who runs Mothli, from the server's settings (`legal_name`, `country`,
 * `contact_email`), or null while any is missing: the pages then say Mothli is not open for
 * sign-ups here. `plan`: the two prices as Paddle's catalog has them (`currency`, and `monthly`
 * and `yearly` in the currency's smallest unit), or null where they could not be read: the page
 * then shows the plan without figures. Nothing here is written in the app, and no script from
 * anywhere else is loaded for it. */
import { formatMinor, perMonthOf, savingOf } from "../ui/planFigures";

export type Operator = { legal_name: string; country: string; contact_email: string };
export type CatalogPlan = { currency: string; monthly: string; yearly: string };
export type Site = { operator: Operator | null; plan: CatalogPlan | null };

const WHOLE = /^[1-9]\d{0,14}$/;
const TEXT = (value: unknown): value is string => typeof value === "string" && value.trim() !== "";

function readOperator(value: unknown): Operator | null {
  const found = value as Partial<Operator> | null;
  if (!found || !TEXT(found.legal_name) || !TEXT(found.country) || !TEXT(found.contact_email)) return null;
  return { legal_name: found.legal_name, country: found.country, contact_email: found.contact_email };
}

function readPlan(value: unknown): CatalogPlan | null {
  const found = value as Partial<CatalogPlan> | null;
  if (!found || typeof found.currency !== "string" || !/^[A-Z]{3}$/.test(found.currency)) return null;
  if (typeof found.monthly !== "string" || !WHOLE.test(found.monthly)) return null;
  if (typeof found.yearly !== "string" || !WHOLE.test(found.yearly)) return null;
  return { currency: found.currency, monthly: found.monthly, yearly: found.yearly };
}

/** The public read; rejects when the server cannot be reached or answers anything else. */
export async function fetchSite(): Promise<Site> {
  const response = await fetch("/api/site", { credentials: "same-origin", headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error(`site ${response.status}`);
  const body = (await response.json()) as { operator?: unknown; plan?: unknown };
  return { operator: readOperator(body?.operator), plan: readPlan(body?.plan) };
}

/** The figures the front page shows, worked out from the two catalog prices alone, the same way
 * as the plan sheet: the yearly plan as a month and its saving against twelve months. Null if
 * the currency cannot be formatted here. */
export type PlanShown = { monthly: string; yearlyTotal: string; yearlyPerMonth: string; saving: number | null };

export function planShown(plan: CatalogPlan | null): PlanShown | null {
  if (!plan) return null;
  const monthly = Number(plan.monthly);
  const yearly = Number(plan.yearly);
  const monthlyText = formatMinor(monthly, plan.currency);
  const yearlyText = formatMinor(yearly, plan.currency);
  const perMonth = perMonthOf(yearly, plan.currency);
  if (!monthlyText || !yearlyText || !perMonth) return null;
  return { monthly: monthlyText, yearlyTotal: yearlyText, yearlyPerMonth: perMonth, saving: savingOf(monthly, yearly) };
}
