import gzip
from io import BytesIO
import json
from pathlib import Path
import tarfile
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from pymysql import OperationalError

from models import db
from models.file import ChallengeFile
from models.settings import Settings
from services.backup_paths import backup_dir
from services.backup_scheduler import BackupScheduler
from tests.ui_fixture import app, seed


API = '/admin/backups/api'
TOKEN = {'X-CSRF-Token': 'backup-test-token'}


class PrivateBackupTests(unittest.TestCase):
    def setUp(self):
        seed()
        root = Path(self.enterContext(TemporaryDirectory(dir=Path.cwd())))
        self.uploads = root / 'uploads'
        self.private = root / 'private'
        self.legacy = self.uploads / 'backups'
        self.legacy.mkdir(parents=True)
        self.enterContext(patch.dict(app.config, {'UPLOAD_FOLDER': self.uploads, 'BACKUP_DIR': self.private}))
        self.client = app.test_client()
        self.dump = gzip.compress(b'-- Fixture database dump')

    def login(self, user):
        with self.client.session_transaction() as session:
            session.update(_user_id=str(user), _fresh=True, _csrf_token=TOKEN['X-CSRF-Token'])

    def upload(self, name='backup_round_1.sql.gz', data=None):
        response = self.client.post(f'{API}/upload', headers=TOKEN, data={
            'backup_file': (BytesIO(self.dump if data is None else data), name),
        })
        response.request.input_stream.close()
        return response

    def test_migration_is_repeatable_and_preserves_originals_on_collision(self):
        (self.legacy / 'backup_round_1.sql.gz').write_bytes(self.dump)
        (self.legacy / 'backup_round_1.json').write_text('{"auto_backup": true}', encoding='utf-8')
        (self.legacy / 'backup_round_1_uploads.tar.gz').write_bytes(b'archive')
        (self.legacy / 'unrelated.txt').write_text('leave in place', encoding='utf-8')
        self.private.mkdir()
        (self.legacy / 'backup_conflict.sql.gz').write_bytes(b'different from private copy')
        (self.legacy / 'backup_conflict.json').write_text('{"auto_backup": true}', encoding='utf-8')
        (self.private / 'backup_conflict.sql.gz').write_bytes(self.dump)
        runner = app.test_cli_runner()
        with self.assertLogs(app.logger, level='WARNING'):
            for _ in range(2):
                self.assertEqual(runner.invoke(args=['migrate-legacy-backups']).exit_code, 0)
        self.assertEqual({path.name for path in self.legacy.iterdir()},
                         {'unrelated.txt', 'backup_conflict.sql.gz', 'backup_conflict.json'})
        self.assertEqual((self.private / 'backup_round_1.sql.gz').read_bytes(), self.dump)
        self.assertEqual((self.private / 'backup_round_1_uploads.tar.gz').read_bytes(), b'archive')
        self.assertEqual((self.private / 'backup_conflict.sql.gz').read_bytes(), self.dump)
        self.login(1)
        data = self.client.get(f'{API}/list').json
        by_name = {item['backup_name']: item for item in data['backups']}
        self.assertTrue(by_name['backup_round_1']['auto_backup'])
        self.assertEqual(self.client.get('/files/backups/backup_conflict.sql.gz').status_code, 404)

    def test_backup_directory_cannot_overlap_public_storage(self):
        with app.app_context():
            for path in (self.legacy, Path(app.static_folder) / 'backup-fixture', self.uploads.parent):
                with self.subTest(path=path), patch.dict(app.config, {'BACKUP_DIR': path}):
                    with self.assertRaises(ValueError):
                        backup_dir()

    def test_attachment_requires_login_and_keeps_the_original_filename(self):
        relative_path = 'challenges/1/packet-capture.txt'
        target = self.uploads / relative_path
        target.parent.mkdir(parents=True)
        target.write_text('challenge attachment', encoding='utf-8')
        (self.uploads / 'unregistered.txt').write_text('private upload', encoding='utf-8')
        with app.app_context():
            db.session.add(ChallengeFile(
                challenge_id=1, uploaded_by=1, original_filename='Packet Capture.txt', stored_filename=target.name,
                filepath=str(target), relative_path=relative_path, mime_type='text/plain',
            ))
            db.session.commit()
        path = f'/files/{relative_path}'
        self.assertEqual(self.client.get(path).status_code, 302)
        self.login(2)
        with self.client.get(path) as response:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, b'challenge attachment')
            self.assertIn('attachment; filename="Packet Capture.txt"', response.headers['Content-Disposition'])
            self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertEqual(self.client.get('/files/unregistered.txt').status_code, 404)

    def test_admin_download_only_and_mutations_require_csrf(self):
        self.private.mkdir()
        (self.private / 'backup_round_1.sql.gz').write_bytes(self.dump)
        download = f'{API}/download/backup_round_1'
        self.assertEqual(self.client.get(download).status_code, 302)
        self.login(2)
        self.assertEqual(self.client.get(download).status_code, 302)
        self.assertEqual(self.client.post(f'{API}/delete', json={'backup_name': 'backup_round_1'}).status_code, 403)
        self.login(1)
        with self.client.get(download) as response:
            self.assertEqual(response.data, self.dump)
            self.assertEqual(response.mimetype, 'application/gzip')
            self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertEqual(self.client.post(f'{API}/delete', json={'backup_name': 'backup_round_1'}).status_code, 403)
        self.assertTrue((self.private / 'backup_round_1.sql.gz').exists())

    def test_uploaded_backup_does_not_overwrite_and_deletes_sidecars(self):
        self.login(1)
        response = self.upload()
        self.assertTrue(response.json['success'])
        self.assertEqual(self.upload(data=gzip.compress(b'new')).status_code, 409)
        self.assertEqual((self.private / 'backup_round_1.sql.gz').read_bytes(), self.dump)
        (self.private / 'backup_round_1_redis.rdb').write_bytes(b'cache')
        response = self.client.post(f'{API}/delete', headers=TOKEN, json={'backup_name': 'backup_round_1'})
        self.assertTrue(response.json['success'])
        self.assertEqual(list(self.private.iterdir()), [])
        self.assertFalse(self.client.post(f'{API}/restore', headers=TOKEN, json={'backup_name': 1}).json['success'])

    def test_manual_and_scheduled_backups_include_team_data_in_private_storage(self):
        self.login(1)
        (self.uploads / 'attachment.txt').write_text('team attachment', encoding='utf-8')
        (self.legacy / 'backup_older.sql.gz').write_bytes(self.dump)
        connection = MagicMock()
        cursor = connection.cursor.return_value
        cursor.fetchone.return_value = ('teams', 'CREATE TABLE teams (name VARCHAR(80))')
        cursor.fetchall.side_effect = [[('teams',)], [('Packet Inspectors',)], [('name',)]] * 2
        with patch('pymysql.connect', return_value=connection):
            response = self.client.post(f'{API}/create', headers=TOKEN, json={'include_uploads': True})
            self.assertTrue(response.json['success'], response.json)
            with app.app_context():
                Settings.set('backup_include_uploads', True, 'bool')
            scheduler = BackupScheduler()
            scheduler.app = app
            scheduler.create_automatic_backup()
        dumps = list(self.private.glob('*.sql.gz'))
        self.assertEqual(len(dumps), 2)
        for dump in dumps:
            name = dump.name.removesuffix('.sql.gz')
            with gzip.open(dump, 'rt') as stream:
                self.assertRegex(stream.read(), r"INSERT INTO `teams` \(`name`\) VALUES\s+\('Packet Inspectors'\);")
            with tarfile.open(self.private / f'{name}_uploads.tar.gz') as archive:
                self.assertIn('uploads/attachment.txt', archive.getnames())
                self.assertNotIn('uploads/backups/backup_older.sql.gz', archive.getnames())
            metadata = json.loads((self.private / f'{name}.json').read_text(encoding='utf-8'))
            self.assertTrue(metadata['components']['uploads'])

    def test_restore_handles_legacy_user_columns_and_rolls_back_on_error(self):
        self.login(1)
        legacy = (
            'CREATE TABLE `users` (\n  `id` int,\n  `team_id` int\n);\n'
            'INSERT INTO `users` VALUES (2, 1);'
        )
        self.assertTrue(self.upload(data=gzip.compress(legacy.encode())).json['success'])
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [('users',)]
        data = {'backup_name': 'backup_round_1'}
        with patch('pymysql.connect', return_value=connection):
            self.assertTrue(self.client.post(f'{API}/restore', headers=TOKEN, json=data).json['success'])
            cursor.execute.assert_any_call('INSERT INTO `users` (`id`, `team_id`) VALUES (2, 1);')
            connection.commit.assert_called_once()
            connection.reset_mock()
            cursor.execute.side_effect = (None, None, None, OperationalError('fixture failure'))
            self.assertFalse(self.client.post(f'{API}/restore', headers=TOKEN, json=data).json['success'])
            connection.rollback.assert_called_once()
            connection.commit.assert_not_called()
