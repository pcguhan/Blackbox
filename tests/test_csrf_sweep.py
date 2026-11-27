import re
import unittest
from urllib.parse import urlsplit
from unittest.mock import patch

from tests.ui_fixture import app, seed


SAFE_METHODS = {'GET', 'HEAD', 'OPTIONS'}
JSON_HEADERS = {'Accept': 'application/json'}


def rule_path(rule):
    values = {name: 1 if name.endswith('_id') else 'fixture' for name in rule.arguments}
    _, path = rule.build(values, append_unknown=False)
    return path


class CSRFRouteSweep(unittest.TestCase):
    def setUp(self):
        seed()
        self.enterContext(patch('docker.from_env'))
        self.enterContext(patch('docker.DockerClient'))
        self.anonymous = app.test_client()
        self.player = self.signed_in_client(2, 'player')
        self.admin = self.signed_in_client(1, 'admin')

    def signed_in_client(self, user_id, role):
        client = app.test_client()
        page = client.get('/login')
        token = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)
        response = client.post('/login', data={
            'username': f'preview-{role}', 'password': 'PreviewOnly123!', 'csrf_token': token,
        })
        self.assertEqual(response.status_code, 302)
        with client.session_transaction() as session:
            self.assertEqual(session['_user_id'], str(user_id))
        return client

    def test_every_unsafe_http_method_requires_the_session_token(self):
        # Socket.IO requests are handled outside the Flask URL map. Tokenized
        # email verification and the legacy logout link are GET flows.
        checked = 0
        for rule in app.url_map.iter_rules():
            for method in sorted(rule.methods - SAFE_METHODS):
                path = rule_path(rule)
                checked += 1
                for role, client in (('anonymous', self.anonymous), ('player', self.player), ('admin', self.admin)):
                    with self.subTest(method=method, path=path, role=role):
                        response = client.open(path, method=method, headers=JSON_HEADERS, json={})
                        self.assertIn(response.status_code, (400, 403))
                        self.assertEqual(response.json['message'], 'CSRF token invalid')
        self.assertGreater(checked, 0)

    def test_every_admin_url_denies_players_even_with_a_valid_token(self):
        with self.player.session_transaction() as session:
            headers = {**JSON_HEADERS, 'X-CSRFToken': session['_csrf_token']}
        for rule in app.url_map.iter_rules():
            if not rule.rule.startswith('/admin/'):
                continue
            path = rule_path(rule)
            for method in sorted(rule.methods - {'HEAD', 'OPTIONS'}):
                with self.subTest(method=method, path=path):
                    response = self.player.open(path, method=method, headers=headers)
                    if method == 'GET':
                        self.assertEqual(response.status_code, 302)
                        self.assertEqual(response.location, '/')
                    else:
                        self.assertEqual(response.status_code, 403)

    def test_invalid_tokens_are_rejected_in_form_and_headers(self):
        with self.player.session_transaction() as session:
            player_token = session['_csrf_token']
        for path, client in (
            ('/admin/settings/background-theme', self.admin),
            ('/hints/1/unlock', self.player),
            ('/api/notifications/1/read', self.player),
        ):
            for options in (
                {'data': {'csrf_token': 'non-ascii-\N{LATIN SMALL LETTER E WITH ACUTE}'}},
                {'headers': {'X-CSRFToken': 'invalid'}},
                {'headers': {'X-CSRF-Token': 'invalid'}},
            ):
                with self.subTest(path=path, options=options):
                    headers = {**JSON_HEADERS, **options.get('headers', {})}
                    response = client.post(path, headers=headers, data=options.get('data'))
                    self.assertEqual(response.status_code, 403)
                    self.assertEqual(response.json['message'], 'CSRF token invalid')
        response = self.admin.post('/admin/settings/background-theme', headers={
            **JSON_HEADERS, 'X-CSRFToken': player_token,
        })
        self.assertEqual(response.status_code, 403)

    def test_login_redirects_stay_local(self):
        for destination, expected in (
            ('/challenges/1?tab=hints', '/challenges/1?tab=hints'),
            ('//example.test/path', '/challenges/'),
            ('/\\example.test/path', '/challenges/'),
        ):
            with self.subTest(destination=destination):
                client = app.test_client()
                login = client.get('/login', query_string={'next': destination})
                token = re.search(r'name="csrf_token" value="([^"]+)"', login.text).group(1)
                response = client.post('/login', query_string={'next': destination}, data={
                    'username': 'preview-player', 'password': 'PreviewOnly123!', 'csrf_token': token,
                })
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.location, expected)
                self.assertFalse(urlsplit(response.location).netloc)
