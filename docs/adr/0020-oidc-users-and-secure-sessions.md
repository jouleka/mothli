# ADR 0020 — OIDC users and secure hosted sessions

**Status:** Accepted (2026-07-16; LIT-40)

## Context

ADR 0017 requires hosted identity to resolve to an application UUID rather than an email address or
provider subject. ADR 0018 supplied `users`, `external_identities`, and `sessions`, but intentionally
left them inaccessible to HTTP. LIT-40 adds the authentication bootstrap without prematurely exposing
the tenant-owned feature routes assigned to LIT-41.

## Decision

### Authorization Code with PKCE and signed ID tokens

Hosted login uses OIDC Authorization Code with a fresh 256-bit `state`, `nonce`, PKCE verifier, and
independent browser-binding cookie for every attempt. PKCE is always S256. A short-lived PostgreSQL
row stores only digests of state/browser tokens plus the verifier, nonce, exact configured issuer,
safe relative return path, and thirty-minute expiry (ten minutes until 2026-11-24; see the
note at the end). Callback consumption is one atomic `DELETE`, so
success, provider refusal, and validation failure all make an attempt non-replayable.

Discovery must exactly repeat the configured HTTPS issuer and advertise code flow, S256, an HTTPS
authorization/token/JWKS endpoint, and a pinned signed ID-token algorithm. The default allow-list is
`RS256`; `none` is forbidden. Token exchange uses `client_secret_basic`. ID tokens are verified with
`joserfc`, with one bounded JWKS refresh for key rotation, before issuer, subject, audience/`azp`,
expiry, issued-at, optional not-before, and constant-time nonce checks. Invalid cases return one
generic response (since 2026-11-24 a redirect to the front page, see the note at the end) and never
create an identity or session. Provider access/refresh tokens are not
persisted.

### Internal identity is `(issuer, subject) -> UUID`

One globally unique `(issuer, subject)` mapping resolves one application-generated `users.id`. A
first login atomically creates the user/mapping; later logins return the same UUID. Display name,
email, and email-verification status are profile data only. Email is neither queried nor constrained
as an account key: migration 0006 replaces the old unique active-email index with a non-unique
profile index, so two provider subjects reporting the same address remain separate users. Explicit
identity linking remains out of scope.

### Opaque server-side sessions and session-derived CSRF

The browser receives a 256-bit opaque session token in `__Host-litlet-session`. PostgreSQL stores only
its SHA-256 digest and a separate CSRF-token digest. The session cookie is always `Secure`,
`HttpOnly`, `Path=/`, has no `Domain`, and explicitly uses `SameSite=Lax`. The readable
`__Host-litlet-csrf` cookie has the same Secure/Path/SameSite policy; a state-changing request must
also send it as `X-CSRF-Token`, and both raw values plus the session-bound digest must match.

Sessions have a thirty-day absolute lifetime and a fourteen-day idle lifetime by default (they
were eight hours and thirty minutes until 2026-11-24; see the note at the end). Each
successful lookup checks account state, deletion, revocation, absolute expiry, and idle expiry, then
advances `last_seen_at`. Invalid/expired sessions are revoked. A successful login revokes the old
browser session before issuing a new identifier. `POST /api/auth/logout` requires CSRF, revokes the
server row, clears both cookies, and immediately makes reuse return `401`. This is application logout;
provider-wide single logout is not claimed.

### A narrow pre-owner database role

RLS cannot derive `app.owner_id` until a session or identity has been resolved. The auth process
therefore uses a separate non-superuser `BYPASSRLS` login whose table privileges are limited to:

| Table | Privileges |
| --- | --- |
| `users` | `SELECT, INSERT, UPDATE, DELETE` (delete is only for a losing concurrent-create candidate) |
| `external_identities` | `SELECT, INSERT, UPDATE` |
| `sessions` | `SELECT, INSERT, UPDATE` |
| `oidc_login_attempts` | `SELECT, INSERT, DELETE` |

Startup refuses a superuser, inherited/member role, cluster/database administration capability, a
role without `BYPASSRLS`, a missing required privilege, or any DML privilege on another table. Auth
SQL is schema-qualified. The DSN must be supplied through `HOSTED_AUTH_DSN`; migrations do not
create a password or grant a general application role. Owner-scoped repositories in LIT-41 use a
different role and transaction-local RLS context.

### Hosted composition remains fail-closed

`DEPLOYMENT_MODE=local` is unchanged and exposes no authentication routes. In hosted mode this ticket
exposes only `/api/health/live` and `/api/auth/{login,callback,session,logout}`. Existing book,
position, ingestion, and companion routes return `404` until LIT-41 composes owner-scoped
repositories. Consequently the only hosted state-changing browser endpoint in this increment is
logout, and it is CSRF-protected. This ticket does not authorize public deployment.

**Implementation amendment (LIT-41):** ADR 0021 subsequently opens the checked-in owner-scoped
library/read/reset/cost inventory. Upload, deletion, export, storage, and worker surfaces remain
closed; the identity/session rules in this record are unchanged.

## Configuration

Hosted startup requires `HOSTED_AUTH_DSN`, `OIDC_ISSUER`, `OIDC_CLIENT_ID`,
`OIDC_CLIENT_SECRET`, and the exact HTTPS `OIDC_REDIRECT_URI`. Optional settings pin scopes/signing
algorithms, transport timeout/clock skew, authorization-attempt expiry, and absolute/idle session
lifetimes. DSNs and client secrets are `SecretStr` values and are never logged or returned.

## Verification

The real PostgreSQL harness provisions a unique restricted auth role, applies every migration, and
runs a signed mock OIDC provider through the real discovery, PKCE, token-exchange, JWKS, and JWT
validation code. It proves stable UUID resolution, same-email separation, cookie flags, digest-only
storage, browser-bound one-time state, nonce/issuer/audience/`azp`/time/signature rejection, session
rotation, idle expiry, CSRF failure and success, logout revocation, and hosted feature-route closure.
Local FastAPI tests prove the default composition remains unchanged.

## Consequences and boundaries

The login/session boundary is production-shaped but the hosted application remains incomplete.
LIT-41 owns authenticated owner-scoped feature repositories and route dependencies; later tickets own
object storage, workers, encrypted BYOK, distributed quotas, cross-tenant endpoint coverage, account
deletion/export, deployment headers/TLS, and operational rollout. The accepted local SQLite library
is neither read nor migrated by this work.

## Staying signed in (2026-11-24)

The owner's decision: readers should not come back signed out. A reader now stays signed in on their
own device for 30 days at most, and is signed out after 14 days without touching Mothli; until now
it was 8 hours at most and 30 minutes untouched, and the settings refused more than one day
untouched. Mothli is hosted-only (ADR 0043), so these are the defaults rather than settings the
server must carry. The idle lifetime may now be set up to 30 days, and still never above the absolute
lifetime. Nothing else changes: the cookies were already persistent (`Max-Age` is the absolute
lifetime, so they outlive a closed browser) and keep their `Secure`, `HttpOnly` and `SameSite=Lax`
flags; each login still rotates the session; CSRF is still bound to the session row; signing out
and asking to delete the account still revoke at once. The sign-in service's own session may be
shorter than Mothli's; Mothli does not depend on it once a reader is signed in.

## Time at the sign-in service, and a failed return (2026-11-24)

On launch day the owner pressed sign in, came back from the sign-in service after more than ten
minutes, and got a bare page of JSON (`Login could not be completed`, HTTP 400): the attempt had
expired, and nothing was logged, so finding why took a database query. Three changes:

- A reader now has 30 minutes (`OIDC_ATTEMPT_TTL_SECONDS` defaults to 1800, the most the setting
  allows; the browser-binding cookie's `Max-Age` follows it). Resetting a password by email or
  finding a phone for a code easily takes more than ten minutes. The longer window weakens
  nothing that matters: the attempt is still one use only, bound to the browser by its own cookie,
  protected by PKCE and the nonce, and checked against the exact issuer.
- Every failed return (no state or cookie, attempt missing or expired, the provider's `error`, no
  code, exchange or validation failure, inactive account) is a `303` with `no-store` to the fixed
  path `/?signin=again`, still clearing the browser-binding cookie and creating no session. The
  path is a literal, never built from the request, and does not say which step failed. The front
  page sees the marker, says kindly that the sign-in did not finish with the button right there,
  and takes the marker out of the address (it says nothing to a reader already signed in).
- Each failed return writes one static warning naming only the stage, from a fixed table checked by
  the ADR 0029 log gate: no state, code, cookie, email, subject or address. A provider's `error` is
  logged only as one of a few standard OAuth codes, or `other`.

Expired attempts nobody returned for were already removed whenever a new sign-in starts (the same
transaction that adds the new attempt, with the auth role's existing `DELETE`); a test now holds it.
