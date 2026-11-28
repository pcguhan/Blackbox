# Administrator guide

This guide covers the admin console (`/admin`). For deployment, environment variables and backups, see [operations.md](operations.md). For scoring rules, see [scoring-and-security.md](scoring-and-security.md).

## Avoiding lockout

- **Administrators can always use local username/password sign-in**, even when Google-only mode is on and when the access list is enabled. Keep at least one administrator with a known local password.
- Administrators are exempt from the Exclusive Access list.
- If you lose admin access entirely, an operator can promote an account in the database (`UPDATE users SET is_admin = 1 WHERE username = '...'`) and restart the app.

## Sign-in settings (Settings → Authentication)

| Setting | Key | Default | Effect |
|---|---|---|---|
| Google sign-in | `auth_google_enabled` | On when `GOOGLE_CLIENT_ID` is configured | Shows **Continue with Google**. Requires `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REDIRECT_URI` and a strong `SECRET_KEY`. |
| Google-only participation | `auth_google_only` | Off | Blocks local registration, local login, password reset and verification resend for non-admins on the server, and ends existing local non-admin sessions on their next request. |

Google sign-in accepts only Google accounts whose email is verified and ends in `@vitstudent.ac.in`; this is checked on the server against the signed ID token, not only by Google's account picker. Roll numbers are taken from the Google profile name (pattern `24BCE1234`); if no roll number is present, or the username is already taken, a unique username is generated. When Google-only is off, local registration, SMTP email verification and password recovery work exactly as before.

Email (SMTP) settings are unchanged and remain under **Settings → Email**. Saved SMTP passwords are never displayed.

## Exclusive Access (Settings → Exclusive Access)

An optional allowlist that applies to Google and local accounts alike.

- Upload a `.csv` or `.xlsx` file of email addresses (one per row; case and surrounding spaces are ignored). Choose **Replace** or **Append**, or **Clear** the list.
- Turn the restriction on or off with the toggle (`exclusive_access_enabled`, default off). It does not replace the Google domain check or the Google-only toggle; all three combine.
- Signed-in users who are no longer allowed are signed out on their next request. Administrators are always exempt.

## Appearance (Settings → Appearance)

- Palettes: **Default**, **Graphite**, **Forest**, **Copper**, **Paper**, **Navy/Blue** and **Neon**. Existing saved values keep their meaning; an unknown value falls back to Default.
- Font: choose the site font (the original font remains the default).
- The CYSCOM name, logo and the `background.png` image background are used on participant and admin pages in every palette, with a readability overlay.
- Optional custom background CSS can still be enabled.
- The event logo upload is re-encoded; the CYSCOM logo is used when none is uploaded.

## Event and scoring settings

The **Event** and **Scoring** forms save independently: saving one never changes the other's values.

- **Flag format prefix** (`flag_format_prefix`, default `CYS`): used for new challenge flag placeholders, validation hints and generated Docker flags. Existing flags are never rewritten.
- **Event subtitle**, description, registration, teams and scoreboard visibility.
- **First blood bonus**, decay function and **scoreboard freeze**.

## Teams

The **Teams** page lists every team with captain, member count and score, and allows point adjustments and team deletion. Team details show each member's nickname (local accounts) or name and roll number (Google accounts) separately from the team name.

## Challenges

- The create and edit forms share one layout. New flags default to the `CYS{}` placeholder; regex flags, multiple flags, branching flags, prerequisites, ACTs and hint prerequisites are all retained.
- The challenge list shows solve counts (counted per team in team mode).
- **Testing flags as an admin** never creates a solve, never awards points and never affects first blood. It is logged in the audit log as `ADMIN_TEST_FLAG`.
- Deleting a paid hint or a challenge refunds the unlock costs; deleting a flag keeps the points already earned with it; deleting an attachment removes the stored file.

## Docker challenges

- **Docker Upload** (sidebar): upload a challenge archive (`.zip`, `.tar`, `.tar.gz` or `.tgz` containing a `Dockerfile` and `challenge.yml`; see `static/docker/challenge.yml.example`). The archive is validated (no absolute paths, `..`, links or devices; size and file-count caps), the image is built, and you can create a challenge from it or replace the image of an existing one. Builds run the supplied Dockerfile, so upload only archives you trust.
- **Flag modes**: *dynamic* gives every instance its own HMAC flag (`CYS{...}`), injected as the `FLAG` environment variable and optionally written to a file path; the stored seed and any static guess are rejected. *Static* uses the flag you enter.
- **Docker Config**: CPU/memory limits, instance caps, expiry, restart cooldown, auto-stop on solve and cleanup.
- **Docker Status**: running instances; stopped, missing or expired containers are reconciled automatically.

## Monitoring and exports

Submissions, Users, Hint Logs and Dynamic Flags have filters and **CSV export**. The Users page marks Admin and Player roles and can filter to players only. Flag Sharing (abuse detection) and Branching pages are unchanged.

## Backups and versions

- **Backups** are stored privately (outside the public uploads folder) and can be downloaded only by administrators. See [backups.md](backups.md).
- The **Technology versions** dialog in the admin sidebar lists the platform's component versions for vulnerability checks. It is visible to administrators only.
