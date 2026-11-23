/** The public pages' addresses (ADR 0045 step 8): each a stable path of its own, served the app's
 * page by the proxy (deploy/Caddyfile, `@pages`; a backend test keeps the two lists equal). */
export const PAGE_PATHS = {
  terms: "/terms",
  privacy: "/privacy",
  refunds: "/refunds",
  contact: "/contact",
} as const;

export type PublicPageName = keyof typeof PAGE_PATHS;

/** Which public page an address is, if any. */
export function publicPageAt(pathname: string): PublicPageName | null {
  const found = (Object.keys(PAGE_PATHS) as PublicPageName[]).find((name) => PAGE_PATHS[name] === pathname);
  return found ?? null;
}

/** The page the app shows at `pathname` in place of everything else: a public page in hosted
 * mode (`session` null when signed out, an object when signed in), never in local mode
 * (`session` undefined), which has no accounts, plans or such pages. */
export function routedPublicPage(session: unknown, pathname: string): PublicPageName | null {
  return session === undefined ? null : publicPageAt(pathname);
}
