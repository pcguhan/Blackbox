from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sqlalchemy import inspect, text

from models import db
from models.team import Team
from models.user import User
from scripts.schema_backups import ensure_profile_schema
from tests.test_avatar_security import picture
from tests.ui_fixture import app, seed


API = '/api/profile/settings'
PAGE = '/profile/settings'
TOKEN = 'profile-test-token'


class ProfileSettingsTests(unittest.TestCase):
    def setUp(self):
        seed()
        root = Path(self.enterContext(TemporaryDirectory(dir=Path.cwd())))
        self.folder = root / 'avatars'
        self.enterContext(patch.dict(app.config, {'AVATAR_UPLOAD_FOLDER': self.folder}))
        self.client = app.test_client()

    def login(self):
        with self.client.session_transaction() as session:
            session.update(_user_id='2', _fresh=True, _csrf_token=TOKEN)

    def test_profile_page_and_api_require_signin_and_csrf(self):
        self.assertEqual(self.client.get(PAGE).status_code, 302)
        self.assertEqual(self.client.get(API).status_code, 302)
        self.login()
        self.assertEqual(self.client.post(API, data={'nickname': 'Blocked'}).status_code, 403)
        self.assertIn(b'Nickname (optional)', self.client.get(PAGE).data)
        self.assertIn(b'/profile/settings', self.client.get('/').data)
        self.assertIsNone(self.client.get(API).json['nickname'])

    def test_nickname_edits_preserve_local_identity_and_team_membership(self):
        self.login()
        with app.app_context():
            db.session.get(User, 2).full_name = 'Existing display name'
            db.session.commit()
        response = self.client.post(PAGE, data={'nickname': '  Packet Reader  ', 'csrf_token': TOKEN})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get(API).json['nickname'], 'Packet Reader')
        with app.app_context():
            user = db.session.get(User, 2)
            team = db.session.get(Team, user.team_id)
            self.assertEqual(user.username, 'preview-player')
            self.assertEqual(user.full_name, 'Existing display name')
            self.assertEqual(team.name, 'Packet Inspectors')
            self.assertEqual(team.captain_id, user.id)
            self.assertTrue(user.is_team_captain)
        invalid = self.client.post(API, data={'nickname': 'n' * 81}, headers={'X-CSRF-Token': TOKEN})
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(self.client.get(API).json['nickname'], 'Packet Reader')
        cleared = self.client.post(API, data={'nickname': ''}, headers={'X-CSRF-Token': TOKEN})
        self.assertIsNone(cleared.json['nickname'])

    def test_google_identity_is_displayed_and_nickname_is_preserved(self):
        self.login()
        identity = SimpleNamespace(
            id=2, username='preview-player', nickname='Existing nickname', full_name='Google Student',
            roll_number='24BCE1234', auth_provider='google', avatar_filename=None,
        )
        with patch('routes.profile_settings.current_user', identity):
            page = self.client.get(PAGE)
            self.assertIn(b'Google Student (24BCE1234)', page.data)
            self.assertNotIn(b'name="nickname"', page.data)
            result = self.client.post(API, data={'nickname': 'Unwanted edit'}, headers={'X-CSRF-Token': TOKEN})
            self.assertEqual(result.json['nickname'], 'Existing nickname')

    def test_avatar_can_be_removed(self):
        self.login()
        response = self.client.post(API, data={
            'avatar': (BytesIO(picture()), 'avatar.png'), 'csrf_token': TOKEN,
        })
        response.request.input_stream.close()
        url = response.json['avatar_url']
        self.assertTrue(url)
        self.assertIn(url.encode(), self.client.get('/').data)
        response = self.client.post(API, data={'remove_avatar': '1', 'csrf_token': TOKEN})
        self.assertIsNone(response.json['avatar_url'])
        self.assertEqual(list(self.folder.iterdir()), [])
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_ensure_profile_schema_is_additive_and_idempotent(self):
        with app.app_context():
            original_team = db.session.get(User, 2).team_id
            db.session.remove()
            with db.engine.begin() as connection:
                connection.execute(text('ALTER TABLE users DROP COLUMN nickname'))
                connection.execute(text('ALTER TABLE users DROP COLUMN avatar_filename'))
            ensure_profile_schema()
            ensure_profile_schema()
            columns = {col['name'] for col in inspect(db.engine).get_columns('users')}
            self.assertTrue({'nickname', 'avatar_filename', 'team_id'} <= columns)
            user = db.session.get(User, 2)
            self.assertEqual(user.team_id, original_team)
            self.assertIsNone(user.nickname)
