import io
import re
import unittest
import zipfile
from unittest.mock import patch

from openpyxl import Workbook

from models import db
from models.user import User
from services import exclusive_access as access
from tests.ui_fixture import Settings, app, seed


def xlsx(*rows):
    workbook = Workbook()
    for row in rows:
        workbook.active.append(row)
    workbook.create_sheet('Second').append(['late@vitstudent.ac.in'])
    with io.BytesIO() as output:
        workbook.save(output)
        return output.getvalue()


class AllowlistParsingTests(unittest.TestCase):
    def test_xlsx_reads_all_sheets_and_cells(self):
        data = xlsx(['Name', 'Email'], ['Alice', '  Alice@VITSTUDENT.ac.in '],
                    ['Bob <BOB@example.test>; alice@vitstudent.ac.in', 42], ['bad..email@example.test'])
        self.assertEqual(access.parse_allowlist('List.XLSX', data), (
            ['alice@vitstudent.ac.in', 'bob@example.test', 'late@vitstudent.ac.in'], 1,
        ))

    def test_csv_handles_bom_quotes_and_duplicates(self):
        csv = b'\xef\xbb\xbfname,email\r\n"Player, One"," ONE@example.test "\r\nTwo,one@example.test\r\n'
        self.assertEqual(access.parse_allowlist('list.csv', csv), (['one@example.test'], 0))

    def test_invalid_files_and_archive_limits(self):
        with io.BytesIO() as output:
            with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
                archive.writestr('xl/worksheets/sheet1.xml', b'0' * 1024)
            compressed_data = output.getvalue()
        with patch.object(access, 'MAX_UNCOMPRESSED_BYTES', 100):
            with self.assertRaisesRegex(access.AllowlistError, 'uncompressed'):
                access.parse_allowlist('list.xlsx', compressed_data)

        for name, data in [('list.xls', b'bad'), ('list.xlsx', b'bad'), ('list.csv', b'"unterminated')]:
            with self.subTest(name=name), self.assertRaises(access.AllowlistError):
                access.parse_allowlist(name, data)
        with self.assertRaisesRegex(access.AllowlistError, '5 MB'):
            access.parse_allowlist('list.csv', b'0' * (access.MAX_UPLOAD_BYTES + 1))

    def test_rejects_xml_document_type_declarations(self):
        with io.BytesIO() as output:
            with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
                archive.writestr('unconventional-part', '<!DOCTYPE root><root/>'.encode('utf-16'))
            with self.assertRaisesRegex(access.AllowlistError, 'document type'):
                access.parse_allowlist('list.xlsx', output.getvalue())

    def test_row_cell_and_address_limits(self):
        with patch.object(access, 'MAX_ROWS', 1):
            with self.assertRaisesRegex(access.AllowlistError, 'rows'):
                access.parse_allowlist('list.csv', b'one@example.test\n\n')
            with self.assertRaisesRegex(access.AllowlistError, 'rows'):
                access.parse_allowlist('list.xlsx', xlsx(['one@example.test']))
        with patch.object(access, 'MAX_CELLS', 1):
            with self.assertRaisesRegex(access.AllowlistError, 'cells'):
                access.parse_allowlist('list.csv', b',one@example.test\n')
        with patch.object(access, 'MAX_EMAILS', 1):
            with self.assertRaisesRegex(access.AllowlistError, 'addresses'):
                access.parse_allowlist('list.csv', b'one@example.test,two@example.test\n')


class ExclusiveAccessTests(unittest.TestCase):
    def setUp(self):
        seed()
        self.admin = app.test_client()
        self.player = app.test_client()

    def token(self, client, path):
        page = client.get(path)
        self.assertEqual(page.status_code, 200)
        return re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)

    def login(self, client, username='preview-player', **values):
        return client.post('/login', data={
            'csrf_token': self.token(client, '/login'),
            'username': username,
            'password': 'PreviewOnly123!',
            **values,
        })

    def admin_post(self, action, **data):
        return self.admin.post('/admin/exclusive-access', data={
            'csrf_token': self.token(self.admin, '/admin/settings'), 'action': action, **data,
        }, follow_redirects=True)

    def test_admin_can_upload_append_list_replace_and_clear(self):
        self.assertEqual(self.login(self.admin, 'preview-admin').status_code, 302)
        self.assertIn(b'Exclusive Access', self.admin.get('/admin/settings').data)
        path = '/admin/exclusive-access'
        self.assertFalse(self.admin.get(path).json['enabled'])

        self.admin_post('upload', allowlist_file=(io.BytesIO(xlsx(['player@example.test'])), 'list.xlsx'))
        self.assertEqual(self.admin.get(path).json['emails'], ['late@vitstudent.ac.in', 'player@example.test'])
        upload = b'PLAYER@EXAMPLE.TEST\nother@example.test'
        self.admin_post('upload', allowlist_file=(io.BytesIO(upload), 'list.csv'), keep_existing='on')
        self.assertEqual(self.admin.get(path).json['total'], 3)
        self.admin_post('upload', allowlist_file=(io.BytesIO(upload), 'list.csv'))
        self.assertEqual(self.admin.get(path).json['emails'], ['other@example.test', 'player@example.test'])
        invalid = self.admin_post('upload', allowlist_file=(io.BytesIO(b'no addresses'), 'list.csv'))
        self.assertIn(b'no valid email addresses', invalid.data)
        self.assertEqual(self.admin.get(path).json['total'], 2)
        self.admin_post('clear')
        self.assertEqual(self.admin.get(path).json['emails'], [])

    def test_admin_authorization_and_csrf(self):
        self.assertEqual(self.player.get('/admin/exclusive-access').status_code, 302)
        self.assertEqual(self.login(self.player).status_code, 302)
        token = self.token(self.player, '/profile')
        self.assertEqual(self.player.get('/admin/exclusive-access').status_code, 302)
        denied = self.player.post('/admin/exclusive-access', data={'action': 'enable', 'csrf_token': token})
        self.assertEqual(denied.status_code, 302)
        self.assertEqual(self.login(self.admin, 'preview-admin').status_code, 302)
        denied = self.admin.post('/admin/exclusive-access', data={'action': 'enable'})
        self.assertEqual(denied.status_code, 403)
        self.assertFalse(self.admin.get('/admin/exclusive-access').json['enabled'])

    def test_mode_rechecks_active_session_admin_exemption_and_local_login(self):
        self.assertEqual(self.login(self.player, remember='on').status_code, 302)
        self.assertIsNotNone(self.player.get_cookie('remember_token'))
        self.login(self.admin, 'preview-admin')
        self.admin_post('enable')
        response = self.player.get('/scoreboard/api/data')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json['message'], access.BLOCKED_MESSAGE)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertIsNone(self.player.get_cookie('remember_token'))
        self.assertEqual(self.player.get('/profile').status_code, 302)
        self.assertIn(b'Event going on right now', self.login(self.player).data)
        self.assertEqual(self.admin.get('/admin/settings').status_code, 200)
        self.assertEqual(self.login(app.test_client(), 'preview-admin').status_code, 302)

        self.admin_post('disable')
        self.assertEqual(self.player.get('/profile').status_code, 302)
        self.assertEqual(self.login(self.player).status_code, 302)

    def test_team_membership_does_not_grant_or_remove_access(self):
        with app.app_context():
            teammate = User(username='teammate', email='teammate@example.test',
                            is_verified=True, team_id=1)
            teammate.set_password('PreviewOnly123!')
            db.session.add(teammate)
            db.session.commit()
            access.replace_allowlist(['player@example.test'])
            access.set_enabled(True)

        self.assertEqual(self.login(self.player).status_code, 302)
        self.assertIn(b'Packet Inspectors', self.player.get('/teams/1').data)
        rows = self.player.get('/scoreboard/api/data').json
        self.assertEqual([row['name'] for row in rows], ['Packet Inspectors'])
        self.assertIn(b'Event going on right now', self.login(app.test_client(), 'teammate').data)
        with app.app_context():
            self.assertEqual(User.query.filter_by(username='teammate').one().team_id, 1)
            access.clear_allowlist()
        denied = self.player.get('/teams/1', follow_redirects=True)
        self.assertIn(b'Event going on right now', denied.data)

    def test_local_registration_checks_email_before_creating_account(self):
        with app.app_context():
            Settings.set('require_email_verification', False, 'bool')
            access.replace_allowlist(['listed@example.test'])
            access.set_enabled(True)
            self.assertTrue(access.is_email_allowed(' LISTED@EXAMPLE.TEST '))
            self.assertFalse(access.is_email_allowed('unlisted@example.test'))

        data = {'username': 'new_player', 'password': 'Fixture123!', 'confirm_password': 'Fixture123!'}
        denied = self.player.post('/register', data={
            **data, 'email': 'unlisted@example.test', 'csrf_token': self.token(self.player, '/register'),
        })
        self.assertIn(b'Event going on right now', denied.data)
        with app.app_context():
            self.assertIsNone(User.query.filter_by(username='new_player').first())
        allowed = self.player.post('/register', data={
            **data, 'email': 'Listed@Example.TEST', 'csrf_token': self.token(self.player, '/register'),
        })
        self.assertEqual(allowed.status_code, 302)
        with app.app_context():
            self.assertIsNotNone(User.query.filter_by(username='new_player').first())

    def test_deactivation_revokes_existing_session_and_refuses_login(self):
        self.login(self.player, remember='on')
        self.login(self.admin, 'preview-admin')
        response = self.admin.post('/admin/users/2/toggle-active', data={
            'csrf_token': self.token(self.admin, '/admin/settings'),
        })
        self.assertFalse(response.json['is_active'])
        denied = self.player.get('/profile', follow_redirects=True)
        self.assertIn(access.DEACTIVATED_MESSAGE.encode(), denied.data)
        self.assertIsNone(self.player.get_cookie('remember_token'))
        self.assertIn(access.DEACTIVATED_MESSAGE.encode(), self.login(self.player).data)

    def test_stats_require_login_and_authenticated_html_and_json_are_no_store(self):
        stats = '/scoreboard/api/stats'
        self.assertEqual(self.player.get(stats).status_code, 302)
        self.assertNotEqual(self.player.get('/').headers.get('Cache-Control'), 'no-store')
        self.login(self.player)
        for path in ('/profile', '/scoreboard/', '/scoreboard/api/data', stats):
            with self.subTest(path=path):
                self.assertEqual(self.player.get(path).headers.get('Cache-Control'), 'no-store')
        self.assertEqual(self.player.head('/profile').headers.get('Cache-Control'), 'no-store')
        self.assertEqual(self.player.get(stats).json['total_teams'], 1)
        with self.player.get('/static/js/app.js') as response:
            self.assertNotEqual(response.headers.get('Cache-Control'), 'no-store')
