import base64
import hashlib
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from google.auth import crypt, jwt
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from tests.ui_fixture import app, seed
from models import db
from models.settings import Settings
from models.team import Team
from models.user import User
from scripts.schema_auth import ensure_auth_schema
from services.google_sign_in import JWKS_URL, TOKEN_URL
from utils.email import generate_confirmation_token


STUDENT_EMAIL = 'student@vitstudent.ac.in'
FIXTURE_PASSWORD = 'PreviewOnly123!'


def b64url(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip('=')


def jwk_integer(value):
    return b64url(value.to_bytes((value.bit_length() + 7) // 8, 'big'))


def post(client, url, data=None):
    with client.session_transaction() as session:
        csrf_token = session.setdefault('_csrf_token', 'fixture-csrf')
    return client.post(url, data=data, headers={'X-CSRF-Token': csrf_token})


class GoogleLogin(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
        cls.signer = crypt.RSASigner.from_string(pem, key_id='fixture-key')
        public = key.public_key().public_numbers()
        cls.jwks = {'keys': [dict(kid='fixture-key', kty='RSA', alg='RS256', use='sig',
                                  n=jwk_integer(public.n), e=jwk_integer(public.e))]}

    def setUp(self):
        seed()
        settings = dict(GOOGLE_CLIENT_ID='fixture-client', GOOGLE_CLIENT_SECRET='fixture-secret',
                        GOOGLE_REDIRECT_URI='http://localhost/auth/google/callback')
        self.addCleanup(app.config.update, {key: app.config[key] for key in settings})
        app.config.update(settings)
        self.client = app.test_client()

    def start(self, next_path=None):
        response = self.client.get('/auth/google', query_string={} if next_path is None else {'next': next_path})
        authorization = urlsplit(response.location)
        self.assertEqual(authorization.netloc, 'accounts.google.com')
        return {key: value[0] for key, value in parse_qs(authorization.query).items()}

    def callback(self, *, next_path=None, bad_signature=False, **overrides):
        flow = self.start(next_path)
        now = int(time.time())
        claims = dict(sub='google-student', email=STUDENT_EMAIL, hd='vitstudent.ac.in', email_verified=True,
                      name='Student Name (24bce1234)', iss='https://accounts.google.com',
                      aud='fixture-client', iat=now, exp=now + 3600, nonce=flow['nonce'])
        claims.update(overrides)
        token = jwt.encode(self.signer, claims).decode()
        if bad_signature:
            body, signature = token.rsplit('.', 1)
            altered = 'A' if signature[0] != 'A' else 'B'
            token = f'{body}.{altered}{signature[1:]}'

        with patch('services.google_sign_in.requests.post', return_value=Mock(json=lambda: {'id_token': token})) as exchange, \
                patch('services.google_sign_in.requests.get', return_value=Mock(json=lambda: self.jwks)) as keys:
            response = self.client.get('/auth/google/callback', query_string={'state': flow['state'], 'code': 'fixture-code'})
        self.assertEqual(exchange.call_args.args, (TOKEN_URL,))
        data = exchange.call_args.kwargs['data']
        self.assertEqual(data['grant_type'], 'authorization_code')
        self.assertEqual(data['code'], 'fixture-code')
        self.assertEqual(data['redirect_uri'], flow['redirect_uri'])
        self.assertEqual(data['client_id'], flow['client_id'])
        self.assertNotIn('client_secret', flow)
        self.assertNotEqual(data['code_verifier'], flow['code_challenge'])
        self.assertEqual(b64url(hashlib.sha256(data['code_verifier'].encode()).digest()), flow['code_challenge'])
        keys.assert_called_once_with(JWKS_URL, timeout=10, allow_redirects=False)
        return response

    def assert_signed_out(self):
        with self.client.session_transaction() as session:
            self.assertNotIn('_user_id', session)

    def test_sign_in_persists_identity_and_verified_email_changes_keep_the_same_user(self):
        self.assertIn(b'Continue with Google', self.client.get('/login').data)
        self.assertEqual(self.callback().location, '/challenges/')
        self.assertIn(b'Student Name (24BCE1234)', self.client.get('/profile').data)
        with app.app_context():
            student = User.query.filter_by(google_sub='google-student').one()
            self.assertEqual((student.username, student.roll_number, student.auth_provider), ('24BCE1234', '24BCE1234', 'google'))
            self.assertTrue(student.is_verified)
            self.assertFalse(student.is_admin)
            student_id = student.id
        self.client.get('/logout')
        self.assertEqual(self.callback(email='renamed@vitstudent.ac.in').location, '/challenges/')
        with app.app_context():
            self.assertEqual(db.session.get(User, student_id).email, 'renamed@vitstudent.ac.in')
            self.assertEqual(User.query.count(), 3)

    def test_rejects_unverified_emails_wrong_domain_and_invalid_signed_claims(self):
        for overrides in (
            {'email': 'student@gmail.com'}, {'email': 'student@vitstudent.ac.in.example.test'},
            {'email': 'student@sub.vitstudent.ac.in'}, {'hd': 'example.test'},
            {'email_verified': False}, {'email_verified': 'true'}, {'sub': ''},
            {'iss': 'https://example.test'}, {'aud': 'different-client'}, {'exp': int(time.time()) - 60},
            {'nonce': 'other-session'}, {'azp': 'different-client'}, {'bad_signature': True},
        ):
            with self.subTest(overrides=overrides):
                self.assertEqual(self.callback(**overrides).location, '/login')
                self.assert_signed_out()
        with app.app_context():
            self.assertEqual(User.query.count(), 2)

    def test_state_is_required_expires_and_is_consumed(self):
        flow = self.start()
        self.assertEqual(flow['code_challenge_method'], 'S256')
        self.assertEqual(flow['response_type'], 'code')
        self.assertEqual(flow['scope'], 'openid email profile')
        self.assertEqual(flow['hd'], 'vitstudent.ac.in')
        self.assertNotEqual(flow['state'], flow['nonce'])
        with patch('services.google_sign_in.requests.post') as exchange:
            for state in ('wrong-state', flow['state']):
                response = self.client.get('/auth/google/callback', query_string={'state': state, 'code': 'fixture-code'})
                self.assertEqual(response.location, '/login')
            flow = self.start()
            with self.client.session_transaction() as session:
                session['google_oauth'] = {**session['google_oauth'], 'created_at': time.time() - 601}
            response = self.client.get('/auth/google/callback', query_string={'state': flow['state'], 'code': 'fixture-code'})
            self.assertEqual(response.location, '/login')
            exchange.assert_not_called()
        self.assert_signed_out()

    def test_roll_number_collision_and_missing_number_have_safe_fallbacks(self):
        with app.app_context():
            db.session.get(User, 2).username = '24BCE1234'
            db.session.commit()
        self.assertEqual(self.callback().location, '/challenges/')
        self.client.get('/logout')
        self.assertEqual(self.callback(sub='another-student', email='no.roll@vitstudent.ac.in', name=None).location, '/challenges/')
        with app.app_context():
            student = User.query.filter_by(email=STUDENT_EMAIL).one()
            self.assertEqual(student.roll_number, '24BCE1234')
            self.assertTrue(student.username.startswith('24BCE1234_'))
            other = User.query.filter_by(google_sub='another-student').one()
            self.assertEqual((other.username, other.full_name, other.roll_number), ('no.roll', 'Participant', None))

    def test_link_requires_local_email_verification_and_preserves_account_and_team(self):
        with app.app_context():
            local = db.session.get(User, 2)
            local.email = STUDENT_EMAIL.upper()
            local.is_verified = False
            token = generate_confirmation_token(local.email)
            membership = local.team_id
            db.session.commit()

        self.assertEqual(self.callback().location, '/login')
        self.assertIn(b'Verify your existing account by email', self.client.get('/login').data)
        self.assert_signed_out()
        self.assertEqual(self.client.get(f'/verify-email/{token}').location, '/login')
        self.assertEqual(self.callback().location, '/challenges/')
        with app.app_context():
            user = User.query.filter_by(google_sub='google-student').one()
            self.assertEqual((user.id, user.username, user.team_id), (2, 'preview-player', membership))
            self.assertTrue(user.is_team_captain)
            self.assertTrue(user.check_password(FIXTURE_PASSWORD))
            self.assertEqual(Team.query.one().captain_id, user.id)

    def test_conflicting_subject_email_and_inactive_accounts_cannot_sign_in(self):
        with app.app_context():
            player = db.session.get(User, 2)
            player.email = STUDENT_EMAIL
            player.is_active = False
            db.session.commit()
        self.assertEqual(self.callback().location, '/login')
        self.assertIn(b'deactivated', self.client.get('/login').data)
        with app.app_context():
            player = db.session.get(User, 2)
            player.is_active = True
            player.google_sub = 'different-subject'
            db.session.commit()
        self.assertEqual(self.callback().location, '/login')
        self.assertIn(b'conflicts with an existing account', self.client.get('/login').data)
        self.assert_signed_out()

    def test_safe_next_is_used_for_google_and_password_sign_in(self):
        for target in ('https://example.test', '//example.test', '/%2Fexample.test',
                       '/\\example.test', '/%5Cexample.test', '/%09/example.test'):
            with self.subTest(target=target):
                self.assertEqual(self.callback(next_path=target).location, '/challenges/')
                self.client.get('/logout')
        self.assertEqual(self.callback(next_path='/teams/?tab=join').location, '/teams/?tab=join')
        self.client.get('/logout')
        data = {'username': 'preview-player', 'password': FIXTURE_PASSWORD}
        self.assertEqual(post(self.client, '/login?next=/%252Fevil.test', data).location, '/challenges/')
        self.client.get('/logout')
        self.assertEqual(post(self.client, '/login?next=/teams/', data).location, '/teams/')

    def test_google_only_allows_google_accounts_to_create_and_join_teams(self):
        with app.app_context():
            Settings.set('auth_google_only', True, 'bool')
        self.assertEqual(self.callback().location, '/challenges/')
        created = post(self.client, '/teams/create', {'team_name': 'Google Team'})
        self.assertEqual(created.status_code, 302)
        self.client.get('/logout')
        self.assertEqual(self.callback(sub='second-subject', email='second@vitstudent.ac.in').location, '/challenges/')
        with app.app_context():
            team = Team.query.filter_by(name='Google Team').one()
            team_id, invite_code = team.id, team.invite_code
        joined = post(self.client, '/teams/join/confirm', {'invite_code': invite_code})
        self.assertTrue(joined.json['success'])
        with app.app_context():
            captain, member = User.query.filter(User.team_id == team_id).order_by(User.id).all()
            self.assertTrue(captain.is_team_captain)
            self.assertEqual(member.google_sub, 'second-subject')
            self.assertFalse(member.is_team_captain)

    def test_provider_can_be_disabled_and_pending_callbacks_are_refused(self):
        flow = self.start()
        with app.app_context():
            Settings.set('auth_google_enabled', False, 'bool')
        self.assertNotIn(b'Continue with Google', self.client.get('/login').data)
        self.assertEqual(self.client.get('/auth/google').location, '/login')
        with patch('services.google_sign_in.requests.post') as exchange:
            response = self.client.get('/auth/google/callback', query_string={'state': flow['state'], 'code': 'fixture-code'})
            self.assertEqual(response.location, '/login')
            exchange.assert_not_called()
        self.assert_signed_out()

    def test_google_only_blocks_old_password_sessions_even_after_the_account_is_linked(self):
        with app.app_context():
            db.session.get(User, 2).email = STUDENT_EMAIL
            db.session.commit()
        local_client = app.test_client()
        remembered_client = app.test_client()
        password_form = {'username': 'preview-player', 'password': FIXTURE_PASSWORD, 'remember': 'on'}
        self.assertEqual(post(local_client, '/login', password_form).location, '/challenges/')
        self.assertEqual(post(remembered_client, '/login', password_form).location, '/challenges/')
        with remembered_client.session_transaction() as session:
            session.clear()
        self.assertEqual(self.callback().location, '/challenges/')
        with app.app_context():
            Settings.set('auth_google_only', True, 'bool')
        response = local_client.get('/api/event-status')
        self.assertEqual(response.status_code, 403)
        self.assertIn('must sign in with Google', response.json['message'])
        self.assertIsNone(local_client.get_cookie('remember_token'))
        self.assertIn(b'must sign in with Google', local_client.get('/login').data)
        self.assertEqual(remembered_client.get('/profile').location, '/login')
        self.assertIsNone(remembered_client.get_cookie('remember_token'))
        self.assertEqual(self.client.get('/profile').status_code, 200)

    def test_google_accounts_cannot_add_a_local_password_via_smtp_reset(self):
        self.assertEqual(self.callback().location, '/challenges/')
        self.client.get('/logout')
        with app.app_context():
            token = generate_confirmation_token(STUDENT_EMAIL)
        with patch('gevent.spawn') as send:
            post(self.client, '/forgot-password', {'email': STUDENT_EMAIL})
            send.assert_not_called()
        form = {'password': 'NewPassword123', 'confirm_password': 'NewPassword123'}
        response = post(self.client, f'/reset-password/{token}', form)
        self.assertEqual(response.location, '/login')
        self.assertIn(b'This account signs in with Google', self.client.get('/login').data)


class LocalAuthPolicy(unittest.TestCase):
    def setUp(self):
        seed()
        settings = dict(GOOGLE_CLIENT_ID='fixture-client', GOOGLE_CLIENT_SECRET='fixture-secret',
                        GOOGLE_REDIRECT_URI='http://localhost/auth/google/callback')
        self.addCleanup(app.config.update, {key: app.config[key] for key in settings})
        app.config.update(settings)
        self.client = app.test_client()

    def test_google_only_blocks_each_local_flow_and_hides_participant_links(self):
        with app.app_context():
            Settings.set('auth_google_only', True, 'bool')
            player = db.session.get(User, 2)
            token = generate_confirmation_token(player.email)
        page = self.client.get('/login').data
        self.assertIn(b'Administrator login', page)
        for hidden_link in (b'Register here', b'Forgot Password?', b'Resend Verification Email'):
            self.assertNotIn(hidden_link, page)

        reset_path = f'/reset-password/{token}'
        for path in ('/register', '/forgot-password', reset_path):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).location, '/login')

        forms = {
            '/register': {'username': 'new_player', 'email': 'new@example.test',
                          'password': FIXTURE_PASSWORD, 'confirm_password': FIXTURE_PASSWORD},
            '/forgot-password': {'email': 'player@example.test'},
            reset_path: {'password': 'NewPassword123', 'confirm_password': 'NewPassword123'},
            '/resend-verification': {'email': 'player@example.test'},
        }
        with patch('gevent.spawn') as send:
            for path, form in forms.items():
                with self.subTest(path=path):
                    self.assertEqual(post(self.client, path, form).location, '/login')
            send.assert_not_called()

        response = post(self.client, '/login', {'username': 'preview-player', 'password': FIXTURE_PASSWORD})
        self.assertIn(b'must sign in with Google', response.data)
        with self.client.session_transaction() as session:
            self.assertNotIn('_user_id', session)
        with app.app_context():
            self.assertEqual(User.query.count(), 2)
            self.assertTrue(db.session.get(User, 2).check_password(FIXTURE_PASSWORD))

    def test_admin_toggle_is_admin_only_csrf_protected_and_allows_local_admin_recovery(self):
        endpoint = '/admin/settings/auth'
        settings = {'auth_google_enabled': 'on', 'auth_google_only': 'on'}
        self.assertEqual(post(self.client, endpoint, settings).status_code, 302)
        post(self.client, '/login', {'username': 'preview-player', 'password': FIXTURE_PASSWORD})
        self.assertEqual(post(self.client, endpoint, settings).location, '/')
        self.client.get('/logout')
        with app.app_context():
            self.assertIsNone(Settings.get('auth_google_only'))

        admin_form = {'username': 'preview-admin', 'password': FIXTURE_PASSWORD}
        self.assertEqual(post(self.client, '/login', admin_form).location, '/admin/')
        self.assertIn(b'Administrators can always sign in', self.client.get('/admin/settings').data)
        self.assertEqual(self.client.post(endpoint, data=settings).status_code, 403)
        self.assertEqual(post(self.client, endpoint, {'auth_google_only': 'on'}).location, '/admin/settings')
        with app.app_context():
            self.assertIsNone(Settings.get('auth_google_only'))

        self.assertEqual(post(self.client, endpoint, settings).location, '/admin/settings')
        with app.app_context():
            self.assertTrue(Settings.get('auth_google_only', type='bool'))
            self.assertTrue(Settings.get('auth_google_enabled', type='bool'))
        self.assertEqual(self.client.get('/admin/settings').status_code, 200)
        self.client.get('/logout')
        self.assertEqual(post(self.client, '/login', admin_form).location, '/admin/')
        self.assertEqual(post(self.client, endpoint, {}).location, '/admin/settings')
        self.client.get('/logout')
        self.assertEqual(post(self.client, '/login', {'username': 'preview-player', 'password': FIXTURE_PASSWORD}).location,
                         '/challenges/')

    def test_default_preserves_local_registration_verification_resend_and_reset(self):
        app.config['GOOGLE_CLIENT_ID'] = ''
        self.assertNotIn(b'Continue with Google', self.client.get('/login').data)
        email = 'new@example.test'
        registration = {'username': 'new_local', 'email': email, 'password': FIXTURE_PASSWORD,
                        'confirm_password': FIXTURE_PASSWORD}
        with patch('gevent.spawn') as send:
            self.assertEqual(post(self.client, '/register', registration).location, '/login')
            unverified = post(self.client, '/login', {'username': 'new_local', 'password': FIXTURE_PASSWORD})
            self.assertIn(b'Resend Verification Email', unverified.data)
            self.assertEqual(post(self.client, '/resend-verification', {'email': email}).location, '/login')
            self.assertEqual(send.call_count, 2)
            self.assertEqual(send.call_args.args[2:4], (email, 'Verify your email address'))

            with app.app_context():
                token = generate_confirmation_token(email)
            self.assertEqual(self.client.get(f'/verify-email/{token}').location, '/login')
            self.assertEqual(post(self.client, '/forgot-password', {'email': email}).location, '/login')
            self.assertEqual(send.call_args.args[2:4], (email, 'Password Reset Request'))

        new_password = 'NewPassword123'
        form = {'password': new_password, 'confirm_password': new_password}
        self.assertEqual(post(self.client, f'/reset-password/{token}', form).location, '/login')
        self.assertEqual(post(self.client, '/login', {'username': 'new_local', 'password': new_password}).location,
                         '/challenges/')


class GoogleSchema(unittest.TestCase):
    def test_additive_upgrade_is_idempotent_and_preserves_password_and_team(self):
        engine = create_engine('sqlite://')
        self.addCleanup(engine.dispose)
        with engine.begin() as connection:
            connection.execute(text('CREATE TABLE users (id INTEGER PRIMARY KEY, password_hash TEXT, team_id INTEGER)'))
            connection.execute(text("INSERT INTO users VALUES (1, 'old-hash', 4)"))
        with patch('scripts.schema_auth.db', SimpleNamespace(engine=engine)):
            ensure_auth_schema()
            ensure_auth_schema()
        with engine.begin() as connection:
            row = connection.execute(text('SELECT * FROM users')).one()
            self.assertEqual(tuple(row), (1, 'old-hash', 4, None, None, 'local'))
            indexes = inspect(connection).get_indexes('users')
            self.assertTrue(any(index['unique'] and index['column_names'] == ['google_sub'] for index in indexes))
            connection.execute(text("INSERT INTO users (id, google_sub) VALUES (2, 'subject'), (3, NULL)"))
            with self.assertRaises(IntegrityError):
                connection.execute(text("INSERT INTO users (id, google_sub) VALUES (4, 'subject')"))
