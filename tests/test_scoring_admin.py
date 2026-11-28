"""Admin mutations and the public team leaderboard."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from models import db
from models.audit_log import AuditLog
from models.branching import ChallengeFlag
from models.challenge import Challenge
from models.file import ChallengeFile
from models.hint import Hint, HintUnlock
from models.settings import Settings
from models.submission import Solve
from models.user import User
from services.cache import cache_service
from services.file_storage import file_storage
from services.websocket import socketio
from tests.test_scoring_submissions import FLAG, add_team, add_user, client_for, csrf_headers
from tests.ui_fixture import app, seed


class ScoringAdmin(unittest.TestCase):
    def setUp(self):
        seed()
        with app.app_context():
            db.session.get(Challenge, 1).flag = FLAG
            self.teammate_id = add_user('teammate', 1).id
            db.session.commit()
        self.admin = client_for(1)
        self.player = client_for(2)
        self.teammate = client_for(self.teammate_id)
        self.admin_headers = csrf_headers(self.admin, '/admin/challenges')
        self.player_headers = csrf_headers(self.player)
        self.teammate_headers = csrf_headers(self.teammate)

    def submit(self, flag=FLAG):
        return self.player.post('/challenges/1/submit', data={'flag': flag}, headers=self.player_headers)

    def settings(self, **fields):
        return self.admin.post('/admin/settings/event-config', headers=self.admin_headers, data={
            'teams_enabled': 'on', 'scoreboard_visible': 'on', **fields,
        })

    def me(self):
        return self.player.get('/scoreboard/api/view').json['me']

    def test_paid_hint_deletion_and_challenge_deletion_refund_team(self):
        with app.app_context():
            db.session.get(Hint, 1).cost = 20
            hint = Hint(challenge_id=1, content='Another hint', cost=15, order=2)
            db.session.add(hint)
            db.session.commit()
            other_hint_id = hint.id
        self.assertEqual(self.submit().status_code, 200)
        self.assertEqual(self.me()['score'], 100)

        for hint_id, score in [(1, 80), (other_hint_id, 65)]:
            response = self.teammate.post(f'/hints/{hint_id}/unlock', headers=self.teammate_headers)
            self.assertEqual(response.json['new_score'], score)
            self.assertEqual(self.me()['score'], score)

        self.player.get('/scoreboard/api/stats')
        response = self.admin.post('/admin/hints/1/delete', headers=self.admin_headers)
        self.assertTrue(response.json['success'])
        self.assertIsNone(cache_service.get_stats())
        self.assertEqual(self.me()['score'], 85)
        with app.app_context():
            self.assertEqual(HintUnlock.query.count(), 1)

        self.assertEqual(self.player.get('/scoreboard/api/stats').json['total_challenges'], 1)
        with patch.object(file_storage, 'delete_challenge_files'):
            response = self.admin.post('/admin/challenges/1/delete', headers=self.admin_headers)
        self.assertTrue(response.json['success'])
        self.assertEqual((self.me()['score'], self.me()['solves']), (0, 0))
        self.assertEqual(self.player.get('/scoreboard/api/stats').json['total_challenges'], 0)
        with app.app_context():
            self.assertEqual(HintUnlock.query.count(), 0)

    def test_flag_deletions_keep_static_and_dynamic_override_points(self):
        with app.app_context():
            flags = [ChallengeFlag(challenge_id=1, flag_value=FLAG, points_override=0),
                     ChallengeFlag(challenge_id=1, flag_value='CYS{fixed}', points_override=75)]
            db.session.add_all(flags)
            Settings.set('first_blood_bonus', 20, 'int')
            static_id, dynamic_id = [flag.id for flag in flags]
        self.assertEqual(self.submit().json['points'], 20)
        self.assertEqual(self.me()['score'], 20)
        response = self.admin.post(f'/admin/challenges/flags/{static_id}/delete', headers=self.admin_headers)
        self.assertTrue(response.json['success'])
        self.assertEqual(self.me()['score'], 20)
        with app.app_context():
            self.assertIsNone(Solve.query.one().flag_id)
            Solve.query.delete()
            db.session.get(Challenge, 1).is_dynamic = True
            db.session.commit()
        self.assertEqual(self.submit('CYS{fixed}').json['points'], 95)
        self.assertEqual(self.me()['score'], 95)
        response = self.admin.delete(f'/admin/branching/flags/{dynamic_id}', headers=self.admin_headers)
        self.assertTrue(response.json['success'])
        self.assertEqual(self.me()['score'], 95)
        with app.app_context():
            solve = Solve.query.one()
            self.assertIsNone(solve.flag_id)
            self.assertEqual(solve.get_current_points(), 95)
            self.assertEqual(ChallengeFlag.query.count(), 0)

    def test_deleting_file_and_image_removes_bytes_and_matching_urls(self):
        with TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            ids = []
            with app.app_context():
                challenge = db.session.get(Challenge, 1)
                for filename, is_image in [('notes.txt', False), ('diagram.png', True)]:
                    filepath = root / filename
                    filepath.write_bytes(b'test data')
                    attachment = ChallengeFile(challenge_id=1, original_filename=filename,
                                               stored_filename=filename, filepath=str(filepath),
                                               relative_path=f'challenges/1/{filename}', is_image=is_image)
                    db.session.add(attachment)
                    db.session.flush()
                    ids.append((attachment.id, filepath, is_image))
                    url = attachment.get_download_url()
                    if is_image:
                        challenge.images = json.dumps([{'url': url}, {'url': '/files/keep.png'}])
                    else:
                        challenge.files = json.dumps([url, '/files/keep.txt'])
                db.session.commit()
            for attachment_id, filepath, is_image in ids:
                section = 'images' if is_image else 'files'
                self.player.get('/scoreboard/api/stats')
                response = self.admin.post(f'/admin/challenges/{section}/{attachment_id}/delete',
                                           headers=self.admin_headers)
                self.assertTrue(response.json['success'])
                self.assertFalse(filepath.exists())
                self.assertIsNone(cache_service.get_stats())
            with app.app_context():
                self.assertEqual(ChallengeFile.query.count(), 0)
                challenge = db.session.get(Challenge, 1)
                self.assertEqual(json.loads(challenge.files), ['/files/keep.txt'])
                self.assertEqual(json.loads(challenge.images), [{'url': '/files/keep.png'}])

    def test_adjustments_are_audited_and_settings_refresh_dynamic_points(self):
        with app.app_context():
            challenge = db.session.get(Challenge, 1)
            challenge.is_dynamic = True
            challenge.decay_solves = 4
            for index in range(2):
                team = add_team(f'Other {index}', f'OTHER00{index}')
                db.session.add(Solve(team_id=team.id, challenge_id=1, points_earned=100))
            db.session.add(Solve(user_id=2, team_id=1, challenge_id=1, points_earned=100, is_first_blood=True))
            db.session.commit()
        old = self.me()['score']
        self.player.get('/scoreboard/api/stats')
        self.assertEqual(self.settings(decay_function='parabolic', first_blood_bonus='25').status_code, 302)
        self.assertIsNone(cache_service.get_stats())
        result = self.me()['score']
        self.assertEqual(result, 113)
        self.assertNotEqual(result, old)
        for path, delta, expected in [('/admin/users/2/adjust-points', 10, 123),
                                      ('/admin/teams/1/adjust-points', -5, 118)]:
            response = self.admin.post(path, json={'points': delta, 'reason': 'Correction'},
                                       headers=self.admin_headers)
            self.assertEqual(response.json['new_score'], expected)
            self.assertEqual(self.me()['score'], expected)
        self.assertEqual(self.admin.post('/admin/users/1/adjust-points', json={'points': 10},
                                         headers=self.admin_headers).status_code, 400)
        with app.app_context():
            records = AuditLog.query.filter_by(action='ADJUST_POINTS').order_by(AuditLog.id).all()
            self.assertEqual([record.details for record in records], [
                {'target_user_id': 2, 'points': 10, 'reason': 'Correction'},
                {'target_team_id': 1, 'points': -5, 'reason': 'Correction'},
            ])

    def test_challenge_solve_count_uses_distinct_teams_in_team_mode(self):
        with app.app_context():
            db.session.add_all([Solve(user_id=2, team_id=1, challenge_id=1, points_earned=100),
                                Solve(user_id=self.teammate_id, challenge_id=1, points_earned=100),
                                Solve(user_id=1, challenge_id=1, points_earned=100)])
            db.session.commit()
        for teams, count in [(True, 1), (False, 2)]:
            with app.app_context():
                Settings.set('teams_enabled', teams, 'bool')
            html = self.admin.get('/admin/challenges').text
            row = html[html.index('Inspect the request'):].split('</tr>', 1)[0]
            self.assertRegex(row, rf'<td>\s*{count}\s*</td>')

    def test_team_drilldown_current_standing_live_refresh_and_freeze(self):
        with app.app_context():
            staff_team = add_team('Staff account team', 'STAFF001')
            staff_id = add_user('staff-only', staff_team.id, is_admin=True).id
            db.session.get(User, self.teammate_id).full_name = 'Test Member (26ABC1234)'
            db.session.commit()
        html = self.player.get('/scoreboard/').text
        self.assertIn('Team leaderboard', html)
        self.assertIn('Your standing', html)
        self.assertIn('First bloods', html)
        self.assertNotIn('Staff account team', html)

        with patch.object(User, 'nickname', property(lambda user: 'Nickname' if user.id == 2 else None),
                          create=True), patch.object(User, 'google_sub', property(
                              lambda user: 'test-subject' if user.id == self.teammate_id else None), create=True):
            view = self.player.get('/scoreboard/api/view?nearby=1').json
            self.assertEqual(len(view['rows']), 1)
            self.assertEqual(view['me']['name'], 'Packet Inspectors')
            self.assertEqual(view['me']['rank'], 1)
            self.assertEqual(view['me']['members'], [
                {'name': 'Nickname'}, {'name': 'Test Member', 'roll_number': '26ABC1234'},
            ])

        with patch.object(socketio, 'emit') as emit:
            self.assertEqual(self.settings(scoreboard_frozen='on').status_code, 302)
            emit.assert_any_call('scoreboard_freeze', {'frozen': True},
                                 namespace='/live', to='scoreboard_authenticated')
            emit.reset_mock()
            self.assertEqual(self.submit().status_code, 200)
            self.assertEqual(self.me()['score'], 0)
            self.assertEqual([call.kwargs['to'] for call in emit.call_args_list],
                             ['scoreboard_admin', 'scoreboard_admin'])
            self.assertEqual(self.admin.get('/scoreboard/api/view').json['rows'][0]['score'], 100)

            self.player.get('/scoreboard/api/stats')
            response = self.admin.post(f'/admin/users/{self.teammate_id}/toggle-admin', headers=self.admin_headers)
            self.assertTrue(response.json['is_admin'])
            self.assertIsNone(cache_service.get_stats())
            self.assertEqual(self.me()['members'], [{'name': 'preview-player'}])
            emit.reset_mock()
            self.assertEqual(self.settings(scoreboard_frozen='false').status_code, 302)
            emit.assert_any_call('scoreboard_freeze', {'frozen': False},
                                 namespace='/live', to='scoreboard_authenticated')
            update = next(call for call in emit.call_args_list if call.args[0] == 'scoreboard_update')
            self.assertEqual(update.kwargs['to'], 'scoreboard_authenticated')
            self.assertEqual(update.args[1][0]['score'], 100)
            self.assertEqual(self.me()['score'], 100)

            self.assertEqual(self.admin.post(f'/admin/users/{staff_id}/toggle-admin',
                                             headers=self.admin_headers).status_code, 200)
            rows = self.player.get('/scoreboard/api/data').json
            self.assertIn('Staff account team', [row['name'] for row in rows])
