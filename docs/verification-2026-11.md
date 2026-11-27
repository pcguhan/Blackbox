# Verification notes: November 2026 migration

Dated record of what was checked on this branch (`vit-migration`) and what was not. Results from the source project's own triage report are **not** counted as results for this fork.

## Automated tests (run 27 November 2026, Windows, Python 3.13, SQLite + fakeredis, Docker mocked)

`python -m unittest discover -s tests -t .`: **158 tests, all passing.** The baseline before the migration was 25 tests, also passing.

New coverage, by area:

| Area | Test files |
|---|---|
| Google sign-in, Google-only toggle, linking, roll numbers, admin recovery, Exclusive Access with Google | `test_auth_google.py` |
| Exclusive Access, deactivation, `no-store` | `test_access_exclusive.py` |
| Team scoring: concurrent teammate submits, first blood, tie-break, hint refunds, flag deletion, freeze, admin tests | `test_scoring_submissions.py`, `test_scoring_admin.py` |
| Visibility on every endpoint; CSRF on every POST route | `test_visibility_challenges.py`, `test_visibility_mutations.py`, `test_csrf_sweep.py` |
| Hidden challenge names on other teams' pages and in live broadcasts | `test_integration_hidden_names.py` |
| Docker upload: archive validation, builds, image replacement, `SOLUTION.md` blocking | `test_dockerupload_builder.py`, `test_dockerupload_routes.py` |
| Docker runtime: per-instance flags, caps, ports, cooldown, cleanup, `CHALLENGE_HOST` | `test_dockerruntime_lifecycle.py` |
| Private backups, avatar re-encoding, profile settings, version inventory | `test_backups_private.py`, `test_avatar_security.py`, `test_profile_settings.py`, `test_versions.py` |
| Themes (all old palettes plus Navy/Neon), background image, branding | `test_themes_appearance.py` |
| Participant pages, guide, team pages | `test_participant_pages.py` |
| Admin pages, filters, CSV exports, separate event/scoring saves, `CYS{}` default | `test_adminui_console.py` |

## Not yet performed

- Browser checks (login flows, team pages, challenge submit, live scoreboard, each palette, admin, mobile layout) in a real browser.
- Real Google OAuth sign-in against Google (tests mock the token exchange and keys).
- Real SMTP delivery.
- Docker builds and container runs against a real Docker daemon.
- Migrations and rollbacks on MySQL/MariaDB with a copy of real-shaped data (the SQL files were not executed; SQLite tests cover the ORM schema and the auth schema upgrade).
- Restoring a backup on disposable infrastructure.
