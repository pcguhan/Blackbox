"""Regression tests for the backend review findings (team/scoring, auth, backups, docker)."""
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock

from tests.ui_fixture import app, seed
from tests.test_scoring_submissions import add_team, add_user, client_for, csrf_headers
from models import db
from models.branching import ChallengePrerequisite
from models.challenge import Challenge
from models.settings import DockerSettings, Settings
from models.submission import Solve
from models.user import User
from services.backup_sql import insert_statement, read_backup_inserts, split_sql_statements, sql_literal
from services.scoring import ScoringService

FLAG = 'CYS{review}'


class TeamAndScoring(unittest.TestCase):
    def setUp(self):
        seed()
        with app.app_context():
            db.session.get(Challenge, 1).flag = FLAG
            db.session.commit()
        self.player = client_for(2)
        self.admin = client_for(1)

    def submit(self, client=None):
        client = client or self.player
        return client.post('/challenges/1/submit', data={'flag': FLAG}, headers=csrf_headers(client))

    def test_team_required_switch_saves_the_enforced_key(self):
        headers = csrf_headers(self.admin, '/admin/settings')
        self.admin.post('/admin/settings/event-config', headers=headers,
                        data={'require_team_for_challenges': ['false', 'on']})
        with app.app_context():
            self.assertTrue(Settings.get('require_team_for_challenges', type='bool'))
            self.assertIsNone(Settings.get('team_mode'))

    def test_solo_solve_neither_lowers_value_nor_takes_first_blood_in_team_mode(self):
        with app.app_context():
            challenge = db.session.get(Challenge, 1)
            challenge.is_dynamic, challenge.decay_solves = True, 10
            solo = add_user('solo-player')
            db.session.add(Solve(user_id=solo.id, team_id=None, challenge_id=1, points_earned=100, is_first_blood=False))
            db.session.commit()
            self.assertEqual(challenge.get_solves_count(), 0)
            self.assertEqual(challenge.get_current_points(), challenge.initial_points)
        self.assertTrue(self.submit().json['success'])
        with app.app_context():
            self.assertTrue(Solve.query.filter_by(challenge_id=1, team_id=1).one().is_first_blood)
            board = ScoringService.get_scoreboard(team_based=True)
            self.assertEqual(board[0]['first_bloods'], 1)

    def test_solver_list_follows_teams_enabled(self):
        with app.app_context():
            Settings.set('team_mode', False, 'bool')  # legacy key must not matter
            db.session.add(Solve(user_id=2, team_id=1, challenge_id=1, points_earned=100))
            db.session.commit()
        page = self.player.get('/challenges/1').text
        self.assertIn('Solved by:', page)
        self.assertIn('bi-people-fill', page.split('Solved by:', 1)[1])

    def test_individual_mode_ignores_a_stored_team(self):
        with app.app_context():
            teammate = add_user('teammate', 1)
            db.session.add(Solve(user_id=teammate.id, team_id=1, challenge_id=1, points_earned=100))
            Settings.set('teams_enabled', False, 'bool')
            db.session.commit()
        from services.challenge_catalog import participant_scope
        with app.app_context():
            player = db.session.get(User, 2)
            self.assertIsNone(Solve.query.filter(participant_scope(Solve, player)).first())
        self.assertTrue(self.submit().json['success'])
        with app.app_context():
            self.assertIsNone(Solve.query.filter_by(user_id=2).one().team_id)

    def test_user_can_solve_again_after_switching_teams(self):
        with app.app_context():
            db.session.add(Solve(user_id=2, team_id=1, challenge_id=1, points_earned=100))
            new_team = add_team('Second Team', 'SECOND01')
            db.session.get(User, 2).team_id = new_team.id
            db.session.commit()
            new_team_id = new_team.id
        response = self.submit()
        self.assertTrue(response.json['success'], response.json)
        with app.app_context():
            self.assertEqual(Solve.query.filter_by(challenge_id=1, team_id=new_team_id).count(), 1)

    def test_solver_list_hides_post_freeze_solves(self):
        from services.standings import set_frozen
        with app.app_context():
            set_frozen(True)
            db.session.add(Solve(user_id=2, team_id=1, challenge_id=1, points_earned=100,
                                 solved_at=datetime.utcnow() + timedelta(minutes=5)))
            db.session.commit()
        page = self.player.get('/challenges/1').text
        self.assertNotIn('Solved by:', page)
        self.assertRegex(page, r'<strong>Solves:</strong>\s*0\s*<')
        self.assertIn('Solved by:', self.admin.get('/challenges/1').text)

    def test_circular_prerequisite_rejected(self):
        with app.app_context():
            db.session.add(Challenge(name='Second', description='x', category='Web', flag='CYS{b}', initial_points=50))
            db.session.commit()
        headers = csrf_headers(self.admin, '/admin/settings')
        first = self.admin.post('/admin/branching/prerequisites', headers=headers,
                                data={'challenge_id': '2', 'prerequisite_challenge_id': '1'})
        self.assertTrue(first.json['success'], first.json)
        cycle = self.admin.post('/admin/branching/prerequisites', headers=headers,
                                data={'challenge_id': '1', 'prerequisite_challenge_id': '2'})
        self.assertEqual(cycle.status_code, 400)
        self.assertIn('Circular', cycle.json['message'])
        with app.app_context():
            self.assertEqual(ChallengePrerequisite.query.count(), 1)


class AuthTokensAndLimits(unittest.TestCase):
    def setUp(self):
        seed()
        self.client = app.test_client()

    def test_verification_token_is_not_a_reset_token_and_reset_tokens_are_single_use(self):
        from utils.email import generate_confirmation_token, generate_password_reset_token, verify_password_reset_token
        with app.test_request_context():
            user = User.query.filter_by(username='preview-player').one()
            self.assertIsNone(verify_password_reset_token(generate_confirmation_token(user.email)))
            reset_token = generate_password_reset_token(user)
            self.assertEqual(verify_password_reset_token(reset_token).id, user.id)
            user.set_password('ChangedPassword1!')
            db.session.commit()
            self.assertIsNone(verify_password_reset_token(reset_token))

    def test_login_is_throttled_in_the_app(self):
        from tests.test_auth_google import post
        for _ in range(10):
            post(self.client, '/login', {'username': 'preview-player', 'password': 'wrong-password'})
        response = post(self.client, '/login', {'username': 'preview-player', 'password': 'PreviewOnly123!'})
        self.assertEqual(response.status_code, 429)

    def test_mail_environment_config_is_used(self):
        from utils.email import email_configured, get_mail_settings
        with app.test_request_context():
            app.config.update(MAIL_USERNAME='env-sender@example.test', MAIL_PASSWORD='secret')
            try:
                self.assertEqual(get_mail_settings()['username'], 'env-sender@example.test')
                self.assertTrue(email_configured())
            finally:
                app.config.update(MAIL_USERNAME='', MAIL_PASSWORD='')


class BackupsAndDocker(unittest.TestCase):
    def test_sql_literals_escape_backslashes_and_split_safely(self):
        value = "C:\\temp\\new;\nline 'quoted'; end"
        statement = insert_statement('t', ['a', 'b'], [value, None])
        self.assertNotIn('\n', statement)
        self.assertIn("'C:\\\\temp\\\\new;\\nline \\'quoted\\'; end'", statement)
        self.assertEqual(sql_literal(b'\x00\xff'), "X'00ff'")
        dump = f"-- header\nSET FOREIGN_KEY_CHECKS=0;\n{statement}\nINSERT INTO `t` VALUES ('old;\nstyle'' value');\n"
        statements = split_sql_statements(dump)
        self.assertEqual(statements[1], statement)
        self.assertIn("'old;\nstyle'' value'", statements[2])
        self.assertEqual(read_backup_inserts(dump), statements[1:])

    def test_image_allowlist_is_not_a_loose_prefix(self):
        settings = DockerSettings(allowed_repositories='trusted/image\nctf-*\nregistry.example.com/ctf/\nupload/web:1')
        self.assertTrue(settings.is_image_allowed('trusted/image'))
        self.assertTrue(settings.is_image_allowed('trusted/image:1.2'))
        self.assertFalse(settings.is_image_allowed('trusted/image-malicious'))
        self.assertTrue(settings.is_image_allowed('ctf-web:latest'))
        self.assertTrue(settings.is_image_allowed('registry.example.com/ctf/pwn:2'))
        self.assertFalse(settings.is_image_allowed('registry.example.com/ctfx/pwn'))
        self.assertTrue(settings.is_image_allowed('upload/web:1'))
        self.assertFalse(settings.is_image_allowed('upload/web:2'))

    def test_challenge_network_is_created_without_inter_container_traffic(self):
        from services.container_manager import ContainerOrchestrator
        orchestrator = ContainerOrchestrator()
        orchestrator.docker_client = MagicMock()
        orchestrator.docker_client.networks.list.return_value = []
        with app.app_context():
            orchestrator._ensure_challenge_network()
        options = orchestrator.docker_client.networks.create.call_args.kwargs['options']
        self.assertEqual(options['com.docker.network.bridge.enable_icc'], 'false')

    def test_backup_jobs_use_the_event_timezone(self):
        seed()
        from services.backup_scheduler import BackupScheduler
        scheduler = BackupScheduler()
        scheduler.app = app
        scheduler.scheduler = MagicMock()
        scheduler.scheduler.get_job.return_value = None
        with app.app_context():
            Settings.set('backup_frequency', 'daily', 'string')
            Settings.set('timezone', 'Asia/Kolkata', 'string')
            scheduler._schedule_backups()
        self.assertEqual(str(scheduler.scheduler.add_job.call_args.kwargs['timezone']), 'Asia/Kolkata')


if __name__ == '__main__':
    unittest.main()
