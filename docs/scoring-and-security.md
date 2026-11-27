# Scoring and security semantics

How the platform behaves, as implemented. File references point at the main code paths.

## Team scoring

- With teams enabled (`teams_enabled`, default on), the **team** is the ranking unit. Individual players are never ranked rows; admins never appear in standings.
- Each challenge can be solved **once per team**. Concurrent correct submissions from two members are serialised by a per-team Redis lock plus a database row lock, and the `solves` unique constraints reject any duplicate (`routes/challenges.py`, `services/cache.py`).
- Points use one calculation for displayed and awarded values (`services/scoring.py`). Dynamic challenges decay by the configured function as solves accumulate. A flag with a points override awards that value.
- **First blood** goes to the first team to solve a challenge; the configured bonus is added to that solve.
- **Tie-break**: equal scores are ordered by the earlier last-scoring event (solve or adjustment); teams without solves follow teams with solves.
- **Manual adjustments** (admin, per team) count in the score and the tie-break.
- **Paid hints** are unlocked per team; the cost is deducted from the team. Deleting a hint or its challenge refunds the cost.
- **Deleting a flag** detaches historical solves but keeps the points earned.
- **Freeze**: while frozen, players see the standings snapshot from the moment of the freeze; admins see live standings. Live socket updates respect the freeze and scoreboard visibility (`services/standings.py`, `services/websocket.py`).
- **Admin flag tests** never create solves, points or first bloods; they are audit-logged.

## Membership changes

Solves belong to the team they were made for. A member who leaves keeps their audit history; the points stay with the team. Container instances belong to the player and the team they started under; an instance stays private to its owner after they leave the team.

## Visibility

One rule (`services/challenge_catalog.py`) decides whether a participant can see a challenge, and it is applied to the catalog, detail page, file downloads, hints, containers and submissions. It covers hidden and unpublished challenges, disabled challenges, event status (not started, paused, ended), prerequisites, flag unlocks, ACTs and team requirements. Direct URLs to anything not visible return 404. Admins can preview everything. Other teams' pages and live solve broadcasts do not reveal hidden challenge names.

## Dynamic Docker flags

- Each instance gets an HMAC flag bound to the challenge, the owning player, their team and the instance session (`utils/flag_hmac.py`, `models/container.py`). Validation needs no Redis.
- A flag issued to player or team A is rejected for B. For per-instance challenges, the stored seed and static guesses are rejected; static Docker challenges accept their configured flag.
- `FLAG_HMAC_SECRET` must be set to a long random value in production and never committed.

## Authentication and sessions

- Google OAuth uses state, nonce and PKCE; the ID token signature, issuer, audience, expiry and `email_verified` are verified, and the email domain must be `vitstudent.ac.in` (`services/google_sign_in.py`).
- Accounts are linked to Google only when the local account's email matches and was verified by email. A conflicting Google identity is refused.
- Google-only mode is enforced on the server for every local participant endpoint and for existing local sessions (`services/auth_policy.py`). Administrators keep local login for recovery.
- `next` redirects are restricted to local paths.
- Deactivated users and users removed from the Exclusive Access list are signed out on their next request.
- Nicknames are display labels only; authorisation uses user IDs.

## Request protection

- Every state-changing request needs a CSRF token (form field or `X-CSRF-Token` header). `tests/test_csrf_sweep.py` checks every POST route.
- Non-admins get 403 on all admin mutations.
- Signed-in HTML and JSON responses are sent with `Cache-Control: no-store`, so the browser Back button does not show stale authenticated pages.
- Uploaded avatars and logos are re-encoded with Pillow and served with `nosniff` and a sandboxing CSP.
- Docker archives are treated as untrusted input (see the admin guide).
- Backups are kept outside the public uploads directory; `/files/` serves only registered, visible attachments and never `SOLUTION.md`.

## Abuse monitoring

Flag-sharing detection and its admin page are retained unchanged from the previous release.
