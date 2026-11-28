# Private backups

Flask stores admin and scheduler backups in `BACKUP_DIR`. The default for a
local installation is `instance/backups` under the application root. The
Compose application uses `/var/log/blackbox/backups` on its existing persistent
`logs` volume. If you set an alternate path, keep it on a persistent filesystem
writable by the app and separate from the upload and static directories.

On the first startup after upgrade, the application moves backup dumps,
metadata, and companion uploads/Redis artifacts out of `UPLOAD_FOLDER/backups`.
Each file is published in the private directory before the source is deleted.
Running the move again is safe. If two files have the same name and different
contents, the application logs the conflict and leaves both copies for an
operator to reconcile. After reconciling, rerun
`flask --app app migrate-legacy-backups` in the application's environment.
Both Nginx and Flask block the legacy download path, including files left after
a conflict. Use the admin Backups page to download an application dump.

The standalone `backup.sh` and `restore.sh` scripts still work with their
directory-based backup format on the Compose sidecar's `backups` volume.
Both default to `/var/backups/ctf` and honor `BACKUP_DIR` when invoked directly.

Apply `migrations/2026_11_backups.sql` to an existing MySQL/MariaDB database
when deploying the profile settings feature. App startup also ensures that
the two nullable profile columns exist. The rollback removes those columns
and their stored profile settings.
