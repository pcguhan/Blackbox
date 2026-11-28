# Code map

Where things live, for contributors.

| Area | Files |
|---|---|
| App factory, blueprints, request hooks | `app.py`, `config.py`, `security_utils.py` (CSRF) |
| Local auth, SMTP verification, reset | `routes/auth.py`, `utils/email.py` |
| Google sign-in | `routes/auth_google.py`, `services/google_sign_in.py`, `services/auth_policy.py`, `utils/identity.py`, `routes/auth_admin.py` |
| Exclusive Access | `services/exclusive_access.py`, `models/access_allowlist.py`, `routes/access_admin.py` |
| Teams | `models/team.py`, `routes/teams.py`, `templates/team*.html`, `templates/admin/teams.html` |
| Challenge visibility | `services/challenge_catalog.py` |
| Challenges, flag submission | `routes/challenges.py`, `models/challenge.py`, `models/branching.py` |
| Scoring and standings | `services/scoring.py`, `services/standings.py`, `services/cache.py`, `routes/scoreboard.py`, `static/js/scoreboard.js` |
| Hints | `routes/hints.py`, `models/hint.py` |
| Containers and dynamic flags | `services/container_manager.py`, `services/container_reconciliation.py`, `routes/container.py`, `models/container.py`, `utils/flag_hmac.py` |
| Docker upload | `services/docker_builder.py`, `routes/docker_upload.py`, `templates/admin/docker_upload.html` |
| Backups | `services/backup_paths.py`, `services/backup_scheduler.py`, `services/backup_sql.py`, `backup.sh`, `restore.sh` |
| Profile settings, images | `routes/profile_settings.py`, `services/image_sanitizer.py` |
| Admin console | `routes/admin.py`, `routes/adminui_exports.py`, `services/admin_console.py`, `templates/admin/` (`_layout.html`, `_macros.html`, `_challenge_form.html`, `partials/`) |
| Appearance | `routes/appearance_admin.py`, `static/css/themes-extra.css`, `static/js/theme.js`, `templates/base.html` |
| Participant pages, guide | `templates/`, `routes/guide.py`, `static/css/pages.css` |
| Version inventory | `services/version_inventory.py`, `routes/admin_versions.py`, `static/js/admin-versions.js` |
| Schema changes | `migrations/2026_11_*.sql`, `migrations/rollback/`, `scripts/schema_*.py`, `scripts/db_schema.py` |
| Tests | `tests/` (unittest; fixture in `tests/ui_fixture.py`) |

Run the tests with `python -m unittest discover -s tests -t .` from the repository root.
