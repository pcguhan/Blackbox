"""Filesystem layout for private database backups."""

from contextlib import contextmanager
from filecmp import cmp
import os
from pathlib import Path
import re
from shutil import copy2
import tarfile
from tempfile import TemporaryDirectory

import click
from flask import current_app


ARTIFACT_SUFFIXES = ('.sql.gz', '.sql.json', '.json', '_uploads.tar.gz', '_redis.rdb')


def valid_backup_name(name):
    return (isinstance(name, str) and len(name) <= 200 and not name.lower().endswith('.sql')
            and re.fullmatch(r'backup_[\w. -]+', name) is not None)


def backup_dir():
    config = current_app.config
    directory = Path(config['BACKUP_DIR'] or Path(current_app.instance_path) / 'backups').resolve()
    served_folders = (current_app.static_folder, config['UPLOAD_FOLDER'],
                      config['LOGO_UPLOAD_FOLDER'], config['AVATAR_UPLOAD_FOLDER'])
    for folder in served_folders:
        served = Path(folder).resolve()
        if directory.is_relative_to(served) or served.is_relative_to(directory):
            raise ValueError('BACKUP_DIR must be separate from uploads and static files')
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory


def regular_file(path):
    return not path.is_symlink() and path.is_file()


def delete_backup_files(directory, name):
    for suffix in ARTIFACT_SUFFIXES:
        (directory / f'{name}{suffix}').unlink(missing_ok=True)


@contextmanager
def publish_backup_file(target):
    """Stage a file on the same volume and publish it without replacing an existing backup."""
    with TemporaryDirectory(prefix='.backup-', dir=target.parent) as staging_dir:
        staged = Path(staging_dir) / target.name
        staged.touch(mode=0o600)
        yield staged
        os.link(staged, target)


def archive_uploads(uploads, destination):
    def exclude_legacy(entry):
        return None if entry.name == 'uploads/backups' else entry

    with publish_backup_file(destination) as staged, tarfile.open(staged, 'w:gz') as archive:
        archive.add(uploads, arcname='uploads', filter=exclude_legacy)


def migrate_legacy_backups():
    """Move known backup artifacts from UPLOAD_FOLDER/backups into private storage."""
    legacy = Path(current_app.config['UPLOAD_FOLDER']) / 'backups'
    if legacy.is_symlink() or not legacy.is_dir():
        return

    directory = backup_dir()
    conflicts = set()
    # Move dumps first so a conflicting dump keeps its sidecars in the legacy directory.
    for source in sorted(legacy.iterdir(), key=lambda path: not path.name.endswith('.sql.gz')):
        suffix = next((suffix for suffix in ARTIFACT_SUFFIXES if source.name.endswith(suffix)), None)
        if not suffix or not regular_file(source):
            continue
        name = source.name.removesuffix(suffix)
        if not valid_backup_name(name) or name in conflicts:
            continue
        target = directory / source.name
        try:
            with publish_backup_file(target) as staged:
                copy2(source, staged)
                staged.chmod(0o600)
        except FileNotFoundError:
            continue  # Another worker moved the source.
        except FileExistsError:
            try:
                if not regular_file(target) or not cmp(source, target, shallow=False):
                    current_app.logger.warning('Backup migration name conflict for %s', source.name)
                    if suffix == '.sql.gz':
                        conflicts.add(name)
                    continue
            except FileNotFoundError:
                continue
        source.unlink(missing_ok=True)


def init_backup_paths(app):
    @app.cli.command('migrate-legacy-backups')
    def migrate():
        """Move backups from the old uploads directory into private storage."""
        migrate_legacy_backups()
        click.echo('Legacy backup scan complete. Check the application log for name conflicts.')

    if not app.testing:
        with app.app_context():
            try:
                migrate_legacy_backups()
            except (OSError, ValueError):
                app.logger.exception('Legacy backup migration failed. Retry with the migrate-legacy-backups command.')
