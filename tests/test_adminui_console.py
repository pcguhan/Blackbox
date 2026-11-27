import csv
import io
import re
import unittest
from datetime import datetime
from unittest.mock import patch

from flask import url_for
from werkzeug.datastructures import MultiDict
from werkzeug.exceptions import InternalServerError

from models import db
from models.branching import ChallengeFlag
from models.challenge import Challenge
from models.container import ContainerInstance
from models.hint import Hint, HintUnlock
from models.settings import Settings
from models.submission import Submission
from models.user import User
from services.standings import frozen_snapshot
from tests.ui_fixture import app, seed
from utils.flag_hmac import generate_hmac_flag


class AdminConsoleTests(unittest.TestCase):
    def setUp(self):
        seed()
        self.admin = app.test_client()
        self.player = app.test_client()
        for client, username in ((self.admin, 'preview-admin'), (self.player, 'preview-player')):
            response = client.post('/login', data={
                'username': username, 'password': 'PreviewOnly123!',
                'csrf_token': self.csrf_token(client.get('/login')),
            })
            self.assertEqual(response.status_code, 302)
        self.token = self.csrf_token(self.admin.get('/admin/'))

    @staticmethod
    def csrf_token(response):
        return re.search(r'name="csrf-token" content="([^"]+)"', response.text).group(1)

    def post(self, path, data):
        return self.admin.post(path, data=data, headers={'X-CSRF-Token': self.token})

    def csv_rows(self, path, filename):
        response = self.admin.get(path)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, 'text/csv')
        self.assertEqual(response.headers['Content-Disposition'], f'attachment; filename={filename}.csv')
        return list(csv.DictReader(io.StringIO(response.text)))

    def test_admin_pages_exports_and_team_actions(self):
        with app.test_request_context():
            paths = [url_for(endpoint) for endpoint in (
                'admin.dashboard', 'admin.manage_challenges', 'admin.create_challenge', 'admin.manage_branching',
                'admin.manage_users', 'admin.manage_teams', 'admin.manage_notifications', 'admin.flag_abuse',
                'admin.hint_logs', 'admin.backups', 'admin.settings', 'admin.submissions',
                'admin.ctf_control', 'admin.docker_settings', 'admin.docker_status', 'admin.dynamic_flags_monitor',
            )]
        for path in paths + ['/admin/challenges/1/edit', '/admin/users/2/activity']:
            with self.subTest(path=path):
                response = self.admin.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn('aria-label="Admin navigation"', response.text)
                self.assertIn('href="/admin/teams"', response.text)
                self.assertIn('/static/images/background.png', response.text)
                self.assertIn(self.player.get(path).status_code, (302, 403))

        for export in ('users', 'submissions', 'hint-logs', 'dynamic-flags'):
            with self.subTest(export=export):
                self.assertIn(self.player.get(f'/admin/{export}/export').status_code, (302, 403))

        teams = self.admin.get('/admin/teams')
        self.assertIn('Packet Inspectors', teams.text)
        self.assertIn('preview-player', teams.text)
        response = self.admin.post(
            '/admin/teams/1/adjust-points', json={'points': 15, 'reason': 'Team scoring test'},
            headers={'X-CSRF-Token': self.token},
        )
        self.assertTrue(response.json['success'])
        self.assertEqual(response.json['new_score'], 15)
        dashboard = self.admin.get('/admin/')
        self.assertEqual(dashboard.status_code, 200)
        self.assertNotIn('Team scoring test', dashboard.text)

    def test_admin_errors_render_with_themed_recovery_links(self):
        missing = self.admin.get('/admin/nonexistent')
        self.assertEqual(missing.status_code, 404)
        self.assertIn('Back to dashboard', missing.text)
        with patch('routes.admin.render_template', side_effect=InternalServerError()):
            failed = self.admin.get('/admin/')
        self.assertEqual(failed.status_code, 500)
        self.assertIn('admin-error-code">500', failed.text)
        self.assertIn('Back to dashboard', failed.text)

    def test_users_filter_export_and_activity_preserve_teams(self):
        with app.app_context():
            player = db.session.get(User, 2)
            player.username = '=fixture-player'
            player.full_name = '@Fixture Player'
            player.is_active = False
            db.session.add(Submission(
                user_id=2, team_id=1, challenge_id=1, submitted_flag='local-attempt', is_correct=False,
            ))
            db.session.commit()

        directory = self.admin.get('/admin/users').text
        self.assertIn('admin-role-badge is-admin', directory)
        self.assertIn('admin-role-badge is-player', directory)
        filters = '?role=player&status=disabled&q=PACKET&team_id=1'
        filtered = self.admin.get('/admin/users' + filters).text
        self.assertIn('id="user-2" data-role="player"', filtered)
        self.assertNotIn('id="user-1"', filtered)
        self.assertNotIn('id="user-2"', self.admin.get('/admin/users?q=%25').text)
        activity = self.admin.get('/admin/users/2/activity').text
        for value in ('local-attempt', 'Incorrect', 'Packet Inspectors'):
            self.assertIn(value, activity)

        player, = self.csv_rows('/admin/users/export' + filters, 'users')
        self.assertEqual(player['Role'], 'Player')
        self.assertEqual(player['Status'], 'Disabled')
        self.assertEqual(player['Team'], 'Packet Inspectors')
        self.assertEqual(player['Username'], "'=fixture-player")
        self.assertEqual(player['Full Name'], "'@Fixture Player")

    def test_hint_filters_cover_all_pages_and_respect_platform_dates(self):
        with app.app_context():
            Settings.set('timezone', 'Asia/Kolkata')
            for minute in range(51):
                db.session.add(HintUnlock(
                    hint_id=1, user_id=2, team_id=1, cost_paid=minute,
                    unlocked_at=datetime(2026, 11, 21, 20, minute),
                ))
            db.session.add(HintUnlock(
                hint_id=1, user_id=2, team_id=1, cost_paid=999,
                unlocked_at=datetime(2026, 11, 22, 18, 30),
            ))
            db.session.commit()

        filters = '?user=PREVIEW-PLAYER&team=PACKET&challenge=Inspect&start_date=2026-11-22&end_date=2026-11-22'
        first = self.admin.get('/admin/hint-logs' + filters)
        self.assertIn('id="totalHints">51<', first.text)
        self.assertIn('id="totalPoints">1275<', first.text)
        self.assertIn('end_date=2026-11-22', first.text)
        self.assertIn('2026-11-22 01:30:00', self.admin.get('/admin/hint-logs' + filters + '&page=2').text)
        self.assertIn('id="totalHints">0<', self.admin.get('/admin/hint-logs?user=%25').text)
        self.assertEqual(self.admin.get('/admin/hint-logs/api?team_id=1').json['total'], 52)
        rows = self.csv_rows('/admin/hint-logs/export' + filters, 'hint-logs')
        self.assertEqual(len(rows), 51)
        self.assertEqual({row['Team'] for row in rows}, {'Packet Inspectors'})

    def test_submissions_filter_and_export_same_team_result_and_date(self):
        with app.app_context():
            db.session.add_all([
                Submission(user_id=2, team_id=1, challenge_id=1, submitted_flag='=unsafe', is_correct=False,
                           submitted_at=datetime(2026, 11, 22, 10)),
                Submission(user_id=2, team_id=1, challenge_id=1, submitted_flag='CYS{right}', is_correct=True,
                           submitted_at=datetime(2026, 11, 22, 10)),
                Submission(user_id=2, team_id=1, challenge_id=1, submitted_flag='previous-day', is_correct=False,
                           submitted_at=datetime(2026, 11, 21, 10)),
                Submission(user_id=1, challenge_id=1, submitted_flag='solo-attempt', is_correct=False,
                           submitted_at=datetime(2026, 11, 22, 10)),
            ])
            db.session.commit()
        filters = '?team_id=1&correct_only=0&start_date=2026-11-22'
        result = self.admin.get('/admin/submissions' + filters)
        self.assertIn('1 total', result.text)
        self.assertIn('=unsafe', result.text)
        self.assertNotIn('previous-day', result.text)
        self.assertNotIn('solo-attempt', result.text)
        submission, = self.csv_rows('/admin/submissions/export' + filters, 'submissions')
        self.assertEqual(submission['Team'], 'Packet Inspectors')
        self.assertEqual(submission['Correct'], 'No')
        self.assertEqual(submission['Flag Submitted'], "'=unsafe")

    def test_dynamic_flag_filters_and_verification_use_team_ownership(self):
        with app.app_context():
            challenge = db.session.get(Challenge, 1)
            challenge.docker_enabled = True
            challenge.flag = 'CYS{fixture}'
            team_flag = generate_hmac_flag(1, 1, 2)
            solo_flag = generate_hmac_flag(1, None, 1)
            for label, user_id, team_id, status, flag in (
                ('team', 2, 1, 'running', team_flag), ('solo', 1, None, 'starting', solo_flag),
                ('old', 2, 1, 'stopped', 'CYS{stopped}'),
            ):
                db.session.add(ContainerInstance(
                    challenge_id=1, user_id=user_id, team_id=team_id, container_id=label,
                    container_name=f'{label}-instance', docker_image='fixture-image', port=1234,
                    status=status, session_id=label, expires_at=datetime(2099, 1, 1), dynamic_flag=flag,
                ))
            db.session.commit()

        filters = '?team_id=1&challenge_id=1&status=running'
        with patch('docker.from_env'):
            result = self.admin.get('/admin/dynamic-flags' + filters)
        self.assertEqual(result.status_code, 200)
        self.assertIn(team_flag, result.text)
        self.assertNotIn(solo_flag, result.text)
        container, = self.csv_rows('/admin/dynamic-flags/export' + filters, 'dynamic-flags')
        self.assertEqual(container['Team'], 'Packet Inspectors')
        self.assertEqual(container['Dynamic Flag'], team_flag)

        endpoint = '/admin/dynamic-flags/verify'
        for payload, valid in (
            ({'container_id': 1}, True), ({'container_id': 2}, False),
            ({'user_id': 2, 'challenge_id': 1}, True), ({'user_id': 1, 'challenge_id': 1}, False),
        ):
            with self.subTest(payload=payload):
                response = self.admin.post(
                    endpoint, json={**payload, 'submitted_flag': team_flag}, headers={'X-CSRF-Token': self.token},
                )
                self.assertEqual(response.status_code, 200)
                self.assertIs(response.json['verification']['valid'], valid)

        with app.app_context():
            ContainerInstance.query.delete()
            db.session.commit()
        response = self.admin.post(
            endpoint, json={'user_id': 2, 'challenge_id': 1, 'submitted_flag': team_flag},
            headers={'X-CSRF-Token': self.token},
        )
        self.assertTrue(response.json['verification']['valid'])

    def test_event_and_scoring_saves_only_update_submitted_settings(self):
        path = '/admin/settings/event-config'
        page = self.admin.get('/admin/settings').text
        forms = re.findall(r'<form[^>]*>(.*?)</form>', page, re.S)
        event, = [form for form in forms if 'Save event settings' in form]
        scoring, = [form for form in forms if 'Save scoring settings' in form]
        self.assertNotIn('name="first_blood_bonus"', event)
        self.assertNotIn('name="ctf_name"', scoring)
        self.assertNotIn('name="teams_enabled"', scoring)

        config = MultiDict([
            ('first_blood_bonus', '35'), ('decay_function', 'parabolic'), ('scoreboard_visible', '0'),
            ('scoreboard_frozen', '0'), ('scoreboard_frozen', '1'),
        ])
        self.assertEqual(self.post(path, config).status_code, 302)
        with app.app_context():
            snapshot = frozen_snapshot()
            self.assertTrue(Settings.get('teams_enabled'))
            self.assertIn('teams', snapshot)

        self.assertEqual(self.post(path, {
            'ctf_name': 'CYSCOM CTF', 'event_subtitle': 'Autumn competition', 'ctf_description': 'Team event',
            'flag_format_prefix': 'CYS-TEAM', 'allow_registration': '0',
        }).status_code, 302)
        with app.app_context():
            self.assertEqual(Settings.get('event_subtitle'), 'Autumn competition')
            self.assertEqual(Settings.get('first_blood_bonus'), 35)
            self.assertEqual(Settings.get('decay_function'), 'parabolic')
            self.assertFalse(Settings.get('scoreboard_visible'))
            self.assertFalse(Settings.get('allow_registration'))
            self.assertTrue(Settings.get('teams_enabled'))
            self.assertEqual(frozen_snapshot(), snapshot)
            self.assertIsNone(Settings.get('team_mode'))

        self.assertEqual(self.post(path, {'first_blood_bonus': '20', 'scoreboard_frozen': '0'}).status_code, 302)
        with app.app_context():
            self.assertEqual(Settings.get('ctf_name'), 'CYSCOM CTF')
            self.assertEqual(Settings.get('ctf_description'), 'Team event')
            self.assertEqual(Settings.get('flag_format_prefix'), 'CYS-TEAM')
            self.assertFalse(Settings.get('allow_registration'))
            self.assertIsNone(frozen_snapshot())

    def test_shared_challenge_form_validates_configured_prefix_and_preserves_legacy_flags(self):
        path = '/admin/challenges/create'
        self.assertIn('placeholder="CYS{}"', self.admin.get(path).text)
        with app.app_context():
            Settings.set('act_system_enabled', True, 'bool')
        fields = {
            'name': 'New fixture challenge', 'category': 'web', 'description': 'Local fixture',
            'requires_team': 'true', 'act': 'ACT II', 'unlocks_act': 'ACT III',
        }
        self.assertEqual(self.post(path, {**fields, 'flag': 'VIT{old}'}).status_code, 400)
        self.assertEqual(self.post(path, {**fields, 'flag': 'CYS{}'}).status_code, 400)
        response = self.post(path, {
            **fields, 'flag': 'CYS{new}',
            'additional_flags[]': ['CYS{alternative}', r'OTHER\{[0-9]+\}'],
            'flag_is_regex[]': ['false', 'true'],
            'hint_content[]': ['First hint', 'Second hint'], 'hint_order[]': ['1', '2'],
            'hint_cost[]': ['5', '10'], 'hint_requires[]': ['', '1'],
        })
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            created = Challenge.query.filter_by(name=fields['name']).one()
            self.assertTrue(created.requires_team)
            self.assertEqual((created.act, created.unlocks_act), ('ACT II', 'ACT III'))
            flags = ChallengeFlag.query.filter_by(challenge_id=created.id).order_by(ChallengeFlag.id).all()
            self.assertEqual([flag.is_regex for flag in flags], [False, False, True])
            first, second = Hint.query.filter_by(challenge_id=created.id).order_by(Hint.order).all()
            self.assertEqual(second.requires_hint_id, first.id)
            legacy = db.session.get(Challenge, 1).flag

        save = '/admin/settings/event-config'
        self.assertEqual(self.post(save, {'flag_format_prefix': 'LOCAL'}).status_code, 302)
        self.assertIn('placeholder="LOCAL{}"', self.admin.get(path).text)
        self.assertEqual(self.post(path, {**fields, 'flag': 'CYS{other}'}).status_code, 400)
        self.assertEqual(self.post(path, {**fields, 'flag': 'LOCAL{other}'}).status_code, 302)
        edit = '/admin/challenges/1/edit'
        self.assertIn(f'value="{legacy}"', self.admin.get(edit).text)
        self.assertEqual(self.post(edit, {**fields, 'flag': legacy, 'flag_case_sensitive': 'true'}).status_code, 302)
        with app.app_context():
            self.assertEqual(db.session.get(Challenge, 1).flag, legacy)


if __name__ == '__main__':
    unittest.main()
