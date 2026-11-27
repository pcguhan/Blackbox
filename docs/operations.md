# Operations guide

Deployment, configuration, upgrades and recovery for operators. Use placeholders from `.env.example`; never commit a real `.env`.

## Topology

`docker-compose.yml` runs:

- **web**: Flask app under Gunicorn with the `geventwebsocket` worker (required for Socket.IO websocket upgrades). Keep `WORKERS=1` unless you add a shared Socket.IO message queue: socket sessions live in worker memory.
- **db**: MySQL/MariaDB. **cache**: Redis (caching, solve locks, rate limits).
- **nginx**: reverse proxy, TLS termination and upload limits (`conf/nginx/`). It must forward `Upgrade`/`Connection` headers for `/socket.io/`; the shipped configs do.
- Challenge containers run on the host Docker daemon (see `enable_docker.sh` and the README's Docker section).

## Environment variables (new or changed)

| Variable | Purpose |
|---|---|
| `SECRET_KEY` | Session signing. Must be long and random; Google sign-in stays disabled with a placeholder or short key. |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REDIRECT_URI` | Google OAuth web client. The redirect URI is `https://<your-host>/auth/google/callback`. |
| `MAIL_SERVER`, `MAIL_PORT`, `MAIL_USERNAME`, `MAIL_PASSWORD` | SMTP for local-account verification and password reset (unchanged). |
| `FLAG_HMAC_SECRET` | Key for per-instance Docker flags. Set once; changing it invalidates flags of running instances. |
| `CHALLENGE_HOST` | Hostname shown in challenge connection links. Use a separate host when the main site sends HSTS so plain-HTTP challenge ports stay reachable. Empty = current behaviour. |
| `CHALLENGE_BIND_ADDRESS` | Host address challenge ports bind to (default `0.0.0.0`). |
| `BACKUP_DIR` | Private backup directory (see [backups.md](backups.md)). |
| `MAX_UPLOAD_SIZE` | Request size limit (default 50 MiB). Keep `client_max_body_size` in nginx equal. |

## Google OAuth setup

1. In Google Cloud Console, create an OAuth client of type *Web application*.
2. Add the authorised redirect URI `https://<your-host>/auth/google/callback`.
3. Set the three `GOOGLE_*` variables and a strong `SECRET_KEY`, then restart.
4. In **Admin → Settings → Authentication**, check Google sign-in is enabled. Turn on Google-only only after confirming an administrator can sign in locally.

Only verified `@vitstudent.ac.in` accounts are accepted.

## Upgrading an existing installation

All schema changes in this release are **additive** and keep existing users, teams, solves and settings. **Do not run** the source project's `scripts/individual_schema.py` or its reset scripts; they are not included here and would remove team data.

1. Back up the database (Admin → Backups, or `backup.sh`).
2. Deploy the new image. On start, the entrypoint creates new tables (`access_allowlist`) and `scripts/ensure_docker_schema.py` plus app startup add new nullable columns (Google identity, nickname/avatar, `solves.base_points_override`).
3. Alternatively apply the migrations by hand, in any order:
   `migrations/2026_11_auth.sql`, `2026_11_access.sql`, `2026_11_backups.sql`, `2026_11_scoring.sql`. Each is idempotent.
4. On first start, backups found under `UPLOAD_FOLDER/backups` are moved to `BACKUP_DIR`.

### Rollback

Restore the pre-upgrade image, then (optionally) run the matching files in `migrations/rollback/`. They drop only the columns and tables this release added: Google identity links, nicknames/avatars, the allowlist and the stored fixed-points column. Test a rollback on a copy of the database first.

## Docker challenges

- Admin uploads build images on the host daemon: anyone with admin access can run build steps there. Restrict admin accounts accordingly.
- Example challenges and their `SOLUTION.md` files are not shipped in the image; `.dockerignore` excludes author material.
- Limits (CPU, memory, instance caps, expiry, cooldown) are set in **Admin → Docker Config**.

## TLS and HSTS

Serve the site over HTTPS. If you enable HSTS for the site host, put challenge ports on `CHALLENGE_HOST` (a different hostname), otherwise browsers will refuse plain-HTTP challenge links.

## Backups and restore testing

See [backups.md](backups.md). Test restores only on disposable infrastructure (a separate Compose project or a throwaway VM), never on the live database.

## Logs and troubleshooting

- App logs: `docker compose logs -f web`; Gunicorn logs go to `ACCESS_LOG`/`ERROR_LOG`.
- *Google button missing*: the `GOOGLE_*` variables or a strong `SECRET_KEY` are not set, or the provider is disabled in settings.
- *Live scoreboard not updating*: check nginx forwards websocket upgrade headers and that the `geventwebsocket` worker is in use.
- *"No free ports"*: raise the instance cap or port range in Docker Config, or stop idle instances.

## Local development and tests

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt   # Windows; use .venv/bin/python elsewhere
.venv/Scripts/python -m unittest discover -s tests -t .
```

Tests use SQLite and an in-memory Redis (`fakeredis`); Docker is mocked. They never touch a configured database.
