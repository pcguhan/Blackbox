"""Team scoring through the normal submission and leaderboard entry points."""
from datetime import datetime, timedelta
import re
import unittest
from unittest.mock import patch

from gevent import spawn
from gevent.event import Event
from sqlalchemy.exc import IntegrityError

from tests.ui_fixture import app, seed
from models import db
from models.act_unlock import ActUnlock
from models.audit_log import AuditLog
from models.branching import ChallengeFlag, ChallengePrerequisite, ChallengeUnlock
from models.challenge import Challenge
from models.settings import Settings
from models.submission import Solve, Submission
from models.team import Team
from models.user import User
from services.cache import cache_service
from services.scoring import ScoringService
from services.websocket import socketio

FLAG = 'CYS{scoring}'
LOCK = 'solve_lock:1:team:1'


def client_for(user_id):
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(user_id)
        session['_fresh'] = True
    return client


def csrf_headers(client, path='/challenges/1'):
    response = client.get(path)
    match = re.search(r'name="csrf-token" content="([^"]+)"', response.text)
    return {'X-CSRF-Token': match.group(1)}


def add_user(username, team_id=None, **fields):
    user = User(username=username, team_id=team_id, email=f'{username}@example.test',
                password_hash='unused-test-hash', is_verified=True, **fields)
    db.session.add(user)
    db.session.flush()
    return user


def add_team(name, invite_code):
    team = Team(name=name, invite_code=invite_code)
    db.session.add(team)
    db.session.flush()
    return team


class TeamScoring(unittest.TestCase):
    def setUp(self):
        seed()
        with app.app_context():
            db.session.get(Challenge, 1).flag = FLAG
            self.teammate_id = add_user('teammate', 1).id
            db.session.commit()
        self.player = client_for(2)
        self.teammate = client_for(self.teammate_id)
        self.headers = csrf_headers(self.player)
        self.teammate_headers = csrf_headers(self.teammate)

    def submit(self, flag=FLAG, client=None, headers=None):
        return (client or self.player).post(
            '/challenges/1/submit', data={'flag': flag}, headers=headers or self.headers
        )

    def test_solve_lock_releases_after_wrong_limited_and_failed_attempts(self):
        for flag in ('wrong', 'also wrong'):
            response = self.submit(flag)
            self.assertEqual(response.json['message'], 'Incorrect flag')
            self.assertFalse(cache_service.redis_client.exists(LOCK))
        with app.app_context():
            db.session.get(Challenge, 1).max_attempts = 2
            db.session.commit()
        limited = self.submit(client=self.teammate, headers=self.teammate_headers)
        self.assertIn('Maximum attempts', limited.json['message'])
        self.assertFalse(cache_service.redis_client.exists(LOCK))
        with app.app_context():
            db.session.get(Challenge, 1).max_attempts = 0
            db.session.commit()
        with patch.object(Challenge, 'check_flag', side_effect=RuntimeError('test matcher error')):
            self.assertEqual(self.submit().status_code, 500)
        self.assertFalse(cache_service.redis_client.exists(LOCK))
        with patch.object(cache_service, 'check_rate_limit', return_value=(False, 0)):
            self.assertEqual(self.submit().status_code, 429)
        self.assertFalse(cache_service.redis_client.exists(LOCK))
        self.assertEqual(self.submit().status_code, 200)
        self.assertFalse(cache_service.redis_client.exists(LOCK))
        self.assertEqual(self.submit().status_code, 400)
        self.assertFalse(cache_service.redis_client.exists(LOCK))

    def test_release_leaves_replacement_lease_intact(self):
        redis = cache_service.redis_client
        with cache_service.solve_lock(1, 1, 2) as acquired:
            self.assertTrue(acquired)
            redis.set(LOCK, 'replacement')
        self.assertEqual(redis.get(LOCK), 'replacement')

    def test_two_teammates_submitting_concurrently_award_exactly_once(self):
        entered = Event()
        proceed = Event()
        check_flag = Challenge.check_flag

        def wait_to_match(challenge, *args, **kwargs):
            entered.set()
            if not proceed.wait(timeout=5):
                raise TimeoutError('test did not release the first submit')
            return check_flag(challenge, *args, **kwargs)

        with patch.object(Challenge, 'check_flag', wait_to_match):
            first = spawn(self.submit)
            try:
                self.assertTrue(entered.wait(timeout=5))
                overlapping = self.submit(client=self.teammate, headers=self.teammate_headers)
                self.assertEqual(overlapping.status_code, 429, overlapping.json)
            finally:
                proceed.set()
                first.join(timeout=5)
            self.assertEqual(first.get(timeout=1).status_code, 200)
        self.assertEqual(self.submit(client=self.teammate, headers=self.teammate_headers).status_code, 400)
        self.assertFalse(cache_service.redis_client.exists(LOCK))
        row = self.player.get('/scoreboard/api/view').json['me']
        self.assertEqual((row['score'], row['solves'], row['first_bloods']), (100, 1, 1))
        with app.app_context():
            self.assertEqual(Submission.query.count(), 1)
            self.assertEqual(Solve.query.count(), 1)
            db.session.add(Solve(user_id=self.teammate_id, team_id=1, challenge_id=1, points_earned=100))
            with self.assertRaises(IntegrityError):
                db.session.commit()
            db.session.rollback()

    def test_decay_matches_display_and_team_scores_and_honors_config_fallback(self):
        with app.app_context():
            challenge = db.session.get(Challenge, 1)
            challenge.is_dynamic = True
            challenge.initial_points = 500
            challenge.minimum_points = 100
            challenge.decay_solves = 4
            opponents = []
            for number in range(2):
                team = add_team(f'Opponent {number}', f'OPPONEN{number}')
                opponents.append(add_user(f'opponent{number}', team.id).id)
            Settings.set('first_blood_bonus', 30, 'int')
            db.session.commit()
        self.assertEqual(self.submit().json['points'], 530)
        for user_id in opponents:
            client = client_for(user_id)
            response = self.submit(client=client, headers=csrf_headers(client))
            with app.app_context():
                self.assertEqual(response.json['points'], db.session.get(Challenge, 1).get_current_points())
        for setting, expected in [(None, 400), ('logarithmic', 226)]:
            with self.subTest(setting=setting), patch.dict(app.config, DECAY_FUNCTION='parabolic'):
                with app.app_context():
                    Settings.set('decay_function', setting)
                    challenge = db.session.get(Challenge, 1)
                    self.assertEqual(challenge.get_current_points(), expected)
                    rows = ScoringService.get_scoreboard()
                    self.assertEqual([(row['score'], row['first_bloods']) for row in rows],
                                     [(expected + 30, 1), (expected, 0), (expected, 0)])
                    for row in rows:
                        self.assertEqual(db.session.get(Team, row['id']).get_score(), row['score'])

    def test_admin_test_does_not_affect_team_or_first_blood(self):
        admin = client_for(1)
        headers = csrf_headers(admin)
        with app.app_context():
            db.session.get(User, 1).team_id = 1
            Settings.set('ctf_paused', True, 'bool')
        with patch.object(socketio, 'emit') as emit:
            correct = self.submit(client=admin, headers=headers)
            wrong = self.submit('wrong', client=admin, headers=headers)
            self.assertEqual((correct.status_code, wrong.status_code), (200, 400))
            self.assertTrue(correct.json['test'])
            emit.assert_not_called()
        with app.app_context():
            self.assertEqual(Solve.query.count(), 0)
            self.assertEqual(Submission.query.count(), 0)
            self.assertEqual([event.details['is_correct'] for event in
                              AuditLog.query.filter_by(action='ADMIN_TEST_FLAG').order_by(AuditLog.id)],
                             [True, False])
            Settings.set('ctf_paused', False, 'bool')
            Settings.set('first_blood_bonus', 15, 'int')
        self.assertEqual(self.submit().json['points'], 115)
        view = admin.get('/scoreboard/api/view').json
        self.assertIsNone(view['me'])
        self.assertEqual(view['rows'][0]['members'], [{'name': 'preview-player'}, {'name': 'teammate'}])

    def test_branching_act_and_prerequisites_keep_single_team_solve(self):
        with app.app_context():
            root = db.session.get(Challenge, 1)
            root.unlocks_act = 'ACT II'
            dependent = Challenge(name='Prerequisite', description='Prerequisite test', category='Web',
                                  flag='CYS{prerequisite}', is_hidden=True, unlock_mode='prerequisite')
            path = Challenge(name='Flag path', description='Branch test', category='Web',
                             flag='CYS{hidden}', is_hidden=True, unlock_mode='flag_unlock')
            db.session.add_all([dependent, path])
            db.session.flush()
            db.session.add_all([
                ChallengePrerequisite(challenge_id=dependent.id, prerequisite_challenge_id=1),
                ChallengeFlag(challenge_id=1, flag_value=FLAG),
                ChallengeFlag(challenge_id=1, flag_value='CYS{branch}', unlocks_challenge_id=path.id),
            ])
            Settings.set('act_system_enabled', True, 'bool')
        first = self.submit()
        self.assertEqual(first.status_code, 200, first.json)
        self.assertEqual(first.json['unlocked_act'], 'ACT II')
        self.assertEqual([item['name'] for item in first.json['unlocked_challenges']], ['Prerequisite'])
        branch = self.submit('CYS{branch}', client=self.teammate, headers=self.teammate_headers)
        self.assertEqual(branch.status_code, 200, branch.json)
        self.assertEqual(branch.json['points'], 0)
        self.assertEqual([item['name'] for item in branch.json['unlocked_challenges']], ['Flag path'])
        self.assertEqual(self.submit('CYS{branch}').status_code, 400)
        self.assertFalse(cache_service.redis_client.exists(LOCK))
        with app.app_context():
            self.assertEqual(Solve.query.count(), 1)
            self.assertEqual(ChallengeUnlock.query.one().team_id, 1)
            self.assertEqual(ActUnlock.query.one().team_id, 1)

    def test_ties_use_latest_award_then_put_teams_without_solves_last(self):
        with app.app_context():
            add_team('Second', 'SECOND12')
            add_team('Adjustment only', 'ADJUST12')
            add_team('No activity', 'NOSOLVE1')
            start = datetime(2026, 1, 1)
            db.session.add_all([
                Solve(team_id=1, challenge_id=1, points_earned=100, solved_at=start),
                Solve(team_id=1, points_earned=-100, solved_at=start + timedelta(minutes=3)),
                Solve(team_id=2, challenge_id=1, points_earned=0, solved_at=start + timedelta(minutes=2)),
                Solve(team_id=3, points_earned=0, solved_at=start + timedelta(minutes=1)),
            ])
            db.session.commit()
            rows = ScoringService.get_scoreboard()
            self.assertEqual([row['id'] for row in rows], [3, 2, 1, 4])
            self.assertEqual([row['solves'] for row in rows], [0, 1, 1, 0])

    def test_disabling_teams_makes_two_teammates_independent_entrants(self):
        with app.app_context():
            Settings.set('teams_enabled', False, 'bool')
        self.assertEqual(self.submit().status_code, 200)
        self.assertEqual(self.submit(client=self.teammate, headers=self.teammate_headers).status_code, 200)
        view = self.teammate.get('/scoreboard/api/view?nearby=1').json
        self.assertFalse(view['teams_enabled'])
        self.assertEqual(view['me']['id'], self.teammate_id)
        self.assertEqual([(row['id'], row['score']) for row in view['rows']],
                         [(2, 100), (self.teammate_id, 100)])
        with app.app_context():
            self.assertEqual(Solve.query.filter_by(team_id=None).count(), 2)
            self.assertEqual(db.session.get(Team, 1).get_score(), 0)

    def test_rejected_branch_rolls_back_solve_and_act_unlock(self):
        with app.app_context():
            root = db.session.get(Challenge, 1)
            root.unlocks_act = 'ACT II'
            path = Challenge(name='Existing path', description='Branch test', category='Web',
                             flag='CYS{hidden}', is_hidden=True, unlock_mode='flag_unlock')
            db.session.add(path)
            db.session.flush()
            root_flag = ChallengeFlag(challenge_id=1, flag_value=FLAG)
            branch_flag = ChallengeFlag(challenge_id=1, flag_value='CYS{branch}', unlocks_challenge_id=path.id)
            db.session.add_all([root_flag, branch_flag])
            db.session.flush()
            db.session.add(ChallengeUnlock(challenge_id=path.id, unlocked_by_flag_id=branch_flag.id,
                                           user_id=self.teammate_id, team_id=1))
            Settings.set('act_system_enabled', True, 'bool')

        response = self.submit('CYS{branch}')
        self.assertEqual(response.status_code, 400)
        self.assertIn('already unlocked', response.json['message'])
        self.assertFalse(cache_service.redis_client.exists(LOCK))
        with app.app_context():
            self.assertEqual(Solve.query.count(), 0)
            self.assertEqual(Submission.query.count(), 0)
            self.assertEqual(ActUnlock.query.count(), 0)
        self.assertEqual(self.submit().json['unlocked_act'], 'ACT II')

    def test_schema_ensure_adds_column_idempotently_without_losing_existing_solves(self):
        from sqlalchemy import inspect, text
        from scripts.schema_scoring import ensure_scoring_schema

        self.assertEqual(self.submit().status_code, 200)
        with app.app_context():
            with db.engine.begin() as connection:
                connection.execute(text('ALTER TABLE solves DROP COLUMN base_points_override'))
            ensure_scoring_schema()
            ensure_scoring_schema()
            columns = [column['name'] for column in inspect(db.engine).get_columns('solves')]
            self.assertEqual(columns.count('base_points_override'), 1)
            self.assertEqual(Solve.query.one().points_earned, 100)
