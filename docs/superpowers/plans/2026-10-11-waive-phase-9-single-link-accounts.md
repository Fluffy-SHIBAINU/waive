# Phase 9 — One-link case flow and optional sign-in

> **For agentic workers:** execute task by task, tests first, one commit per task, following `docs/LOOP.md`. Do not start a task that says "needs gate" until `docs/PROGRESS.md` shows the gate closed.

**Goal:** One person on one device finishes a case through one link; a second person is optional; signing in with Google keeps a history of cases across devices.

**Why (first user test, 2026-10-11):** the "Two links" page confused the tester, the review link was shown once and lost with no way back, and the letter step answered "We will read the hospital's answer" after it had already read it. The user approved the redesign and chose: sign-in with Google instead of a remember-me cookie (Apple sign-in was considered and dropped by the user the same day); sign-in optional, offered at the result.

**Spec:** `docs/superpowers/specs/2026-10-02-waive-design.md` (sections on the senior flow, privacy and links). This plan supersedes its two-link start.

## Decisions (user, 2026-10-11)

- Single link by default: the full-scope link (`/c/{token}`) drives every step. `cases/service.authorize` already lets it pass the photo steps.
- Review stays as a screen in the same flow ("Check what we read"), then approve, then packet and reminders.
- Sharing is optional: "Send the photo step to someone else" mints the limited link (`/s/{token}`: add photos, see the result; no review, approve, packet or delete).
- Sign-in: Google only. Optional; offered at the result and on the home page. A case started signed-out can be saved to the account.
- No remember-me cookie. The only cookies are the signed-in session and the short-lived OAuth state.
- Letter step: the page shows what was read (approved / documents requested by a date / denied with an appeal draft).
- Existing `/s/…` and `/c/…` links keep working.

## Screens, in order

1. Home: state, "Start"; "Sign in"; when signed in, "Your cases" (date, hospital, status).
2. Photo of the bill (one button; "Reading your bill…").
3. Is this right? (hospital, amount, date; "Yes" / "No, fix it").
4. Household (big number buttons).
5. Income (photo of the benefit letter, or type the amount).
6. Result (plain answer, "Read this to me", the hospital's rule quoted; "Sign in to keep this case").
7. Check what we read (everything read, the dates that matter; "Fix something" / "Looks right, make my form").
8. Your form and reminders (packet PDF, calendar file, where to send it).
9. Did the hospital answer? (photo of the letter; the outcome on the same page).

On every case page: "Copy my link", "Send the photo step to someone else", "Delete my case".

## Privacy and security rules for sign-in

- OpenID Connect authorization-code flow with PKCE, `state` and `nonce`; scope `openid` only. No email, name or picture is requested or stored.
- Stored per account: provider, an HMAC of the provider's subject id (keyed with `WAIVE_TOKEN_SECRET`), created date. Nothing else.
- Session: random id in an `HttpOnly`, `SameSite=Lax`, `Secure` (production) cookie, 14 days, server-side row, cleared on sign-out and on account deletion. OAuth state cookie: same flags, 10 minutes.
- ID token verified against the provider's JWKS (signature, `iss`, `aud`, `exp`, `nonce`).
- Deleting an account deletes its sessions and every case linked to it. Deleting a case unlinks it.
- Case data stays sealed in the vault; the history page unseals only hospital name and status for display.
- Sign-in is hidden when no provider is configured; the app works without it.
- Shared computers: signing out ends access; nothing about a case remains in the browser except its history entries.

## Global constraints

- Tests first; `uv run ruff format . && uv run ruff check . && uv run pytest` green before every commit.
- No network in tests (`respx`, a locally generated RSA key for ID tokens).
- No secrets in the repo; `.env.example` gains blank `WAIVE_GOOGLE_CLIENT_ID` / `WAIVE_GOOGLE_CLIENT_SECRET`.
- Senior-readable pages: large buttons, short sentences, no typing on the photo steps.
- Keep: sealed case data, expiring and revocable links, no-store / no-referrer / noindex on case and account pages, rate limits, delete.

## Tasks

### 9.1 One link from the start
`POST /cases` creates the case and redirects (303) to `/c/{token}` step 2; the "Two links" page is gone. `cases/service.start_case` still mints both tokens; only the full one is shown. Tests: the redirect target, that Back does not create a second case (GET after redirect), that old `/s/` links still work.

### 9.2 The flow under the full link
Photo, read-back, household, income and result render under `/c/{token}/…` (reuse the senior handlers with a `base` prefix; no duplicated logic). The limited link keeps its own pages. Tests: each step reachable with the full link; the limited link is refused on review, approve, packet, delete.

### 9.3 "Check what we read", approve, packet
The review page becomes step 7 of the same flow, reached from the result by one button; approve leads to step 8 with the packet and the calendar file. Tests: order of screens, fix-and-return, approve once.

### 9.4 Case bar: copy, share, delete
A bar on every case page: "Copy my link" (clipboard, with the link shown as text as a fallback), "Send the photo step to someone else" (shows the limited link with share/copy and what it can do; "Make new links" revokes), "Delete my case". Tests: markup, scopes, revocation.

### 9.5 Letter outcome on the page
`learning/intake.py`: the outcome route returns what was read; the page shows "The hospital approved …", "The hospital asks for: … by …" or "The hospital said no. Reason: … Here is an appeal letter." Remove "We will read the hospital's answer." Tests with the three sample letters' extracted outcomes.

### 9.6 Accounts and sessions (no provider yet)
Tables `accounts`, `sessions`, `account_cases` (via `init_db`; nullable additions through `upgrade_schema`). `auth/accounts.py`: `subject_hash`, `get_or_create`, `link_case`, `cases_of`, `delete_account`; `auth/sessions.py`: create, read, end, purge expired. Tests: hashing is keyed and stable, deletion cascades to cases, expiry.

### 9.7 Google sign-in — needs gate U9.1 for a live test, not for the code
`auth/oidc.py`: authorization URL (PKCE, state, nonce), code exchange, ID-token verification against JWKS (dependency: `pyjwt[crypto]`). Routes `GET /auth/google/start[?case=…]`, `GET /auth/google/callback`, `POST /auth/signout`. Settings `google_client_id`, `google_client_secret`. Tests with `respx` and a local RSA key: happy path, wrong state, wrong nonce, wrong audience, expired token, replayed code.

### 9.8 Home and history
Home shows "Sign in with Google" when configured; signed in: "Your cases" and "Sign out"; result page: "Sign in to keep this case" / "Saved to your account". `GET /account` lists cases; `POST /account/delete`. Tests: listing shows only the account's cases, a case saved after sign-in appears, headers no-store.

### 9.9 (dropped) Apple sign-in
Dropped by the user on 2026-10-11: Google only.

### 9.10 Documents and gallery
README (flow, privacy, settings), `docs/devpost/demo-script.md` (shots follow the one-link flow), `docs/devpost/submission.md`, `waive demo gallery` shot plan and captions, `docs/reports/security-audit.md` addendum (sign-in threat notes), `docs/PROGRESS.md`. Sample hospital letters join `waive demo reset` (task 8.12).

## Exit checks

- A case can be finished start to packet with one link on one device, with no second page to keep.
- The limited link cannot review, approve, read the packet or delete.
- Signed-out use still works end to end; with Google configured, sign-in keeps a history and sign-out ends access.
- No email, name or picture is stored anywhere; `grep` of the schema shows only provider, subject hash and dates.
- Suite green; gallery and documents describe the new flow.
