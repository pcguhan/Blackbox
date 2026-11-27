import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from werkzeug.exceptions import InternalServerError

from models import db
from models.act_unlock import ActUnlock
from models.challenge import Challenge
from models.submission import Solve
from models.team import Team
from models.user import User
from tests import test_ui
from tests.ui_fixture import Settings, app, seed


class ParticipantPages(unittest.TestCase):
    token = test_ui.UIFlows.token
    login = test_ui.UIFlows.login

    def setUp(self):
        seed()
        self.client = app.test_client()

    def add_player(self, username):
        with app.app_context():
            player = User(username=username, email=f'{username}@example.test', is_verified=True)
            player.set_password('PreviewOnly123!')
            db.session.add(player)
            db.session.commit()
            return player.id

    def sign_in(self, username):
        response = self.client.post('/login', data={
            'username': username,
            'password': 'PreviewOnly123!',
            'csrf_token': self.token(),
        })
        self.assertEqual(response.status_code, 302)

    def test_player_pages(self):
        self.login()
        for path, heading in {
            '/': 'Explore the event',
            '/about': 'Rules of engagement',
            '/guide': 'Participant guide',
            '/challenges/': 'Search challenges',
            '/challenges/1': 'Submit Flag',
            '/profile': 'Solved challenges',
            '/teams/': 'Teams',
            '/teams/1': 'Packet Inspectors',
            '/scoreboard/': 'Leaderboard',
        }.items():
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn(heading, response.text)
                self.assertIn('images/background.png', response.text)
                self.assertNotIn('CyberArena', response.text)

    def test_guide_is_public_and_explains_participation(self):
        home = self.client.get('/').text
        self.assertIn('href="/guide"', home)
        self.assertIn('home-credits-list', home)
        self.assertEqual(home.count('class="credit-separator"'), 2)
        response = self.client.get('/guide')
        self.assertEqual(response.status_code, 200)
        for text in ('CYS{...}', 'Teams', 'Local account', 'Google sign-in', '@vitstudent.ac.in',
                     'override', 'Container instances', 'Event timing', 'Scoring and hints'):
            with self.subTest(text=text):
                self.assertIn(text, response.text)
        self.assertIn('href="/register"', response.text)
        self.assertNotIn('Google-only participation is enabled', response.text)

    def test_guide_uses_google_only_mode_and_flag_prefix(self):
        with app.app_context():
            Settings.set('auth_google_only', True, 'bool')
            Settings.set('flag_format_prefix', 'EVENT')
        guide = self.client.get('/guide').text
        self.assertIn('Google-only participation is enabled', guide)
        self.assertIn('Local registration and password sign-in are disabled', guide)
        self.assertNotIn('>create an account</a>', guide)
        self.assertIn('CYS{...}', guide)
        self.assertIn('EVENT{...}', guide)

    def test_challenge_search_markup_and_hidden_card_exclusion(self):
        self.login()
        with app.app_context():
            hidden = Challenge(name='Unrevealed path', description='Unrevealed description',
                               category='Private category', flag='CYS{example}', is_hidden=True)
            db.session.add(hidden)
            db.session.commit()
            hidden_id = hidden.id
        response = self.client.get('/challenges/')
        self.assertEqual(response.status_code, 200)
        for marker in ('type="search" id="challengeSearch"', 'id="categoryFilter"',
                       'id="unsolvedFilter"', 'id="noChallengeMatches"', 'data-solved="false"',
                       'data-search-title="Inspect the request"', 'data-category="Web"'):
            self.assertIn(marker, response.text)
        for secret in ('Unrevealed path', 'Unrevealed description', 'Private category'):
            self.assertNotIn(secret, response.text)
        self.assertEqual(self.client.get(f'/challenges/{hidden_id}').status_code, 302)
        detail = self.client.get('/challenges/1').text
        self.assertIn('placeholder="CYS{...}"', detail)
        self.assertIn('id="hints-container"', detail)

    def test_act_group_unlocks_for_the_team(self):
        self.login()
        with app.app_context():
            Settings.set('act_system_enabled', True, 'bool')
            db.session.add(Challenge(name='Second act challenge', description='A new challenge',
                                     category='Forensics', flag='CYS{example}', act='ACT II'))
            db.session.commit()
        locked = self.client.get('/challenges/').text
        self.assertRegex(locked, r'data-act="ACT II"[^>]*disabled')
        self.assertNotIn('Second act challenge', locked)
        with app.app_context():
            db.session.add(ActUnlock(act='ACT II', team_id=1))
            db.session.commit()
        unlocked = self.client.get('/challenges/').text
        self.assertIn('id="content-ACT-II"', unlocked)
        self.assertIn('data-search-title="Second act challenge"', unlocked)

    def test_profile_names_the_team_and_shows_personal_solves(self):
        self.login()
        with app.app_context():
            db.session.add(Solve(user_id=2, team_id=1, challenge_id=1, points_earned=100))
            db.session.commit()
        with patch.object(User, 'nickname', 'Packet Scout', create=True):
            profile = self.client.get('/profile').text
        self.assertIn('Packet Inspectors', profile)
        self.assertIn('Packet Scout', profile)
        self.assertIn('Team points', profile)
        self.assertIn('Inspect the request', profile)

    def test_team_creation_and_invite_join_share_challenge_progress(self):
        captain_id = self.add_player('test_captain')
        member_id = self.add_player('test_teammate')
        self.sign_in('test_captain')
        create_page = self.client.get('/teams/create')
        self.assertEqual(create_page.status_code, 200)
        for field in ('team_name', 'affiliation', 'country'):
            self.assertIn(f'name="{field}"', create_page.text)
        created = self.client.post('/teams/create', data={
            'team_name': 'Packet Readers', 'affiliation': 'CYSCOM', 'country': 'India',
            'csrf_token': self.token('/teams/create'),
        }, follow_redirects=True)
        self.assertEqual(created.status_code, 200)
        self.assertIn('id="invite-code"', created.text)
        with app.app_context():
            team = Team.query.filter_by(name='Packet Readers').one()
            self.assertEqual(team.captain_id, captain_id)
            team_id, invite_code = team.id, team.invite_code
            db.session.add(Solve(user_id=captain_id, team_id=team_id, challenge_id=1, points_earned=100))
            db.session.commit()

        self.client = app.test_client()
        self.sign_in('test_teammate')
        team_page = f'/teams/{team_id}'
        self.assertNotIn(invite_code, self.client.get(team_page).text)
        token = self.token('/teams/join')
        confirmation = self.client.post('/teams/join', data={'invite_code': invite_code, 'csrf_token': token})
        self.assertEqual(confirmation.status_code, 200)
        self.assertIn('confirmJoin()', confirmation.text)
        joined = self.client.post('/teams/join/confirm', data={'invite_code': invite_code, 'csrf_token': token})
        self.assertTrue(joined.json['success'])
        self.assertEqual(joined.json['redirect'], team_page)
        self.assertIn(invite_code, self.client.get(team_page).text)
        with app.app_context():
            self.assertEqual(db.session.get(User, member_id).team_id, team_id)
        self.assertIn('data-solved="true"', self.client.get('/challenges/').text)
        self.assertNotIn('id="flagForm"', self.client.get('/challenges/1').text)

    def test_countdown_paused_and_ended_views(self):
        self.login()
        with app.app_context():
            Settings.set('ctf_start_time', datetime.utcnow() + timedelta(hours=1), 'datetime')
        countdown = self.client.get('/challenges/')
        self.assertEqual(countdown.status_code, 200)
        self.assertIn('id="countdown"', countdown.text)
        with app.app_context():
            Settings.set('ctf_start_time', datetime.utcnow() - timedelta(hours=1), 'datetime')
            Settings.set('ctf_paused', True, 'bool')
        paused = self.client.get('/challenges/')
        self.assertEqual(paused.status_code, 200)
        self.assertIn('CTF is Paused', paused.text)
        with app.app_context():
            Settings.set('ctf_end_time', datetime.utcnow() - timedelta(minutes=1), 'datetime')
        ended = self.client.get('/challenges/')
        self.assertEqual(ended.status_code, 200)
        self.assertIn('CTF has Ended', ended.text)

    def test_error_pages_keep_their_status_codes(self):
        self.login()
        missing = self.client.get('/page-that-does-not-exist')
        self.assertEqual(missing.status_code, 404)
        self.assertIn('Page not found', missing.text)
        with app.test_request_context('/error-preview'):
            error = app.make_response(app.handle_http_exception(InternalServerError()))
        self.assertEqual(error.status_code, 500)
        self.assertIn('Internal server error', error.get_data(as_text=True))
