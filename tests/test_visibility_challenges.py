from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from models import db
from models.act_unlock import ActUnlock
from models.branching import ChallengeFlag, ChallengePrerequisite, ChallengeUnlock
from models.challenge import Challenge
from models.container import ContainerInstance
from models.file import ChallengeFile
from models.hint import Hint
from models.settings import Settings
from models.team import Team
from models.user import User
from services.container_manager import container_orchestrator
from tests.ui_fixture import app, seed


FLAG = 'CYS{visibility_fixture}'


class ChallengeVisibility(unittest.TestCase):
    def setUp(self):
        seed()
        self.player = self.client_for(2)
        self.admin = self.client_for(1)
        self.headers = {'X-CSRFToken': 'visibility-fixture-token'}
        self.uploads = Path(self.enterContext(TemporaryDirectory(dir=app.root_path)))
        self.enterContext(patch.dict(app.config, {'UPLOAD_FOLDER': str(self.uploads)}))
        self.start = self.enterContext(patch.object(container_orchestrator, 'start_container', return_value={'success': True}))
        self.status = self.enterContext(patch.object(container_orchestrator, 'get_container_status', return_value={'success': True}))
        self.stop = self.enterContext(patch.object(container_orchestrator, 'stop_container', return_value={'success': True}))
        self.revert = self.enterContext(patch.object(container_orchestrator, 'revert_container', return_value={'success': True}))
        self.docker = self.enterContext(patch.object(container_orchestrator, '_ensure_docker_client'))
        self.enterContext(patch.object(container_orchestrator, 'docker_client', None))
        self.enterContext(patch('services.websocket.socketio.emit'))
        with app.app_context():
            db.session.get(Challenge, 1).flag = FLAG
            db.session.commit()

    def client_for(self, user_id):
        client = app.test_client()
        with client.session_transaction() as session:
            session['_user_id'] = str(user_id)
            session['_fresh'] = True
            session['_csrf_token'] = 'visibility-fixture-token'
        return client

    def challenge(self, name, **state):
        challenge = Challenge(name=name, description=f'{name} description', category='Web', flag=FLAG, **state)
        db.session.add(challenge)
        db.session.flush()
        hint = Hint(challenge_id=challenge.id, content=f'{name} hint')
        filename = f'attachment-{challenge.id}.html'
        folder = self.uploads / 'challenges'
        folder.mkdir(exist_ok=True)
        attachment = folder / filename
        attachment.write_text('<p>Challenge attachment</p>', encoding='utf-8')
        db.session.add_all([
            hint,
            ChallengeFile(challenge_id=challenge.id, original_filename='original.html', stored_filename=filename,
                          relative_path=f'challenges\\{filename}', filepath=str(attachment), mime_type='text/html'),
        ])
        db.session.flush()
        return challenge.id, hint.id, f'challenges/{filename}'

    def container(self, challenge_id, team_id=1):
        db.session.add(ContainerInstance(
            challenge_id=challenge_id, user_id=2, team_id=team_id, container_id=f'fixture-{challenge_id}',
            container_name='Fixture container', docker_image='fixture:image', port=8000, status='running',
            session_id=f'fixture-session-{challenge_id}', expires_at=datetime.utcnow() + timedelta(hours=1),
        ))

    def assert_unavailable(self, client, target):
        challenge_id, hint_id, filename = target
        for url in (
            f'/challenges/{challenge_id}', f'/hints/challenge/{challenge_id}',
            f'/files/{filename}', f'/container/status?challenge_id={challenge_id}',
        ):
            with self.subTest(url=url):
                self.assertEqual(client.get(url).status_code, 404)
        for action in ('start', 'stop', 'revert', 'force-cleanup'):
            with self.subTest(action=action):
                response = client.post(f'/container/{action}', headers=self.headers, json={'challenge_id': challenge_id})
                self.assertEqual(response.status_code, 404)
        self.assertEqual(client.post(f'/hints/{hint_id}/unlock', headers=self.headers).status_code, 404)
        for action in ('submit', 'explore'):
            with self.subTest(action=action):
                response = client.post(f'/challenges/{challenge_id}/{action}', headers=self.headers, data={'flag': FLAG})
                self.assertEqual(response.status_code, 404)

    def test_unpublished_hidden_disabled_and_prerequisite_challenges_are_private(self):
        with app.app_context():
            targets = {
                name: self.challenge(name, **state)
                for name, state in (
                    ('Unpublished fixture', {'is_visible': False}),
                    ('Hidden fixture', {'is_hidden': True}),
                    ('Disabled fixture', {'is_enabled': False}),
                    ('Prerequisite fixture', {'unlock_mode': 'prerequisite'}),
                    ('Branch fixture', {'is_hidden': True, 'is_visible': False, 'unlock_mode': 'flag_unlock'}),
                )
            }
            db.session.add(ChallengePrerequisite(challenge_id=targets['Prerequisite fixture'][0], prerequisite_challenge_id=1))
            for challenge_id, _, _ in targets.values():
                self.container(challenge_id)
            db.session.commit()

        catalog = self.player.get('/challenges/')
        self.assertEqual(catalog.status_code, 200)
        self.assertIn('Inspect the request', catalog.text)
        self.assertEqual(self.player.get('/container/my-containers').json['containers'], [])
        for name, target in targets.items():
            with self.subTest(challenge=name):
                self.assertNotIn(name, catalog.text)
                self.assert_unavailable(self.player, target)
                challenge_id, _, filename = target
                self.assertIn(name, self.admin.get('/challenges/').text)
                self.assertIn(f'{name} description', self.admin.get(f'/challenges/{challenge_id}').text)
                self.assertEqual(self.admin.get(f'/hints/challenge/{challenge_id}').json['hints'][0]['content'], f'{name} hint')
                with self.admin.get(f'/files/{filename}') as attachment:
                    self.assertEqual(attachment.status_code, 200)
                    self.assertEqual(attachment.headers['Content-Disposition'], 'attachment; filename=original.html')
                    self.assertEqual(attachment.data, b'<p>Challenge attachment</p>')
        for operation in (self.start, self.status, self.stop, self.revert, self.docker):
            operation.assert_not_called()

    def test_solve_unlocks_act_prerequisite_and_branch_for_current_team_only(self):
        with app.app_context():
            teammate = User(username='teammate', email='teammate@example.test', password_hash='unused', team_id=1)
            outsider = User(username='outsider', email='outsider@example.test', password_hash='unused')
            db.session.add_all([teammate, outsider])
            db.session.flush()
            other_team = Team(name='Other team', invite_code='OTHER123', captain_id=outsider.id)
            db.session.add(other_team)
            db.session.flush()
            outsider.team_id = other_team.id
            teammate_id, outsider_id, other_team_id = teammate.id, outsider.id, other_team.id

            act_target = self.challenge('Next ACT fixture', act='ACT II')
            prereq_target = self.challenge('Prerequisite fixture', is_hidden=True, is_visible=False, unlock_mode='prerequisite')
            branch_target = self.challenge('Branch fixture', is_hidden=True, is_visible=False, unlock_mode='flag_unlock')
            db.session.get(Challenge, 1).unlocks_act = 'ACT II'
            db.session.add_all([
                ChallengePrerequisite(challenge_id=prereq_target[0], prerequisite_challenge_id=1),
                ChallengeFlag(challenge_id=1, flag_value=FLAG, unlocks_challenge_id=branch_target[0]),
                ActUnlock(act='ACT II', user_id=None, team_id=other_team_id),
            ])
            db.session.commit()

        self.assertIn('Next ACT fixture', self.player.get('/challenges/').text)
        with app.app_context():
            Settings.set('act_system_enabled', True, 'bool')
        before = self.player.get('/challenges/').text
        self.assertNotIn('Next ACT fixture', before)
        self.assert_unavailable(self.player, act_target)

        response = self.player.post('/challenges/1/submit', headers=self.headers, data={'flag': FLAG})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json['unlocked_act'], 'ACT II')
        self.assertEqual({row['id'] for row in response.json['unlocked_challenges']}, {prereq_target[0], branch_target[0]})
        teammate_client = self.client_for(teammate_id)
        catalog = teammate_client.get('/challenges/').text
        self.assertIn('Next ACT fixture', catalog)
        self.assertIn('id="content-ACT-II"', catalog)
        self.assertIn('Prerequisite fixture', catalog)
        self.assertIn('Branch fixture', catalog)

        for challenge_id, _, filename in (act_target, prereq_target, branch_target):
            self.assertEqual(teammate_client.get(f'/challenges/{challenge_id}').status_code, 200)
            self.assertEqual(teammate_client.get(f'/hints/challenge/{challenge_id}').status_code, 200)
            with teammate_client.get(f'/files/{filename}') as attachment:
                self.assertEqual(attachment.status_code, 200)
            self.assertEqual(teammate_client.get(f'/container/status?challenge_id={challenge_id}').status_code, 200)

        outsider_client = self.client_for(outsider_id)
        self.assert_unavailable(outsider_client, prereq_target)
        self.assert_unavailable(outsider_client, branch_target)
        for team_id in (other_team_id, None):
            with app.app_context():
                db.session.get(User, 2).team_id = team_id
                db.session.commit()
            self.assert_unavailable(self.player, prereq_target)
            self.assert_unavailable(self.player, branch_target)
        with app.app_context():
            self.assertEqual(ChallengeUnlock.query.one().team_id, 1)

    def test_inactive_event_hides_content_but_preserves_banners_and_admin_preview(self):
        with app.app_context():
            target = self.challenge('Scheduled fixture')
            db.session.commit()
        now = datetime.utcnow()
        for start, end, paused, banner in (
            (now + timedelta(days=1), None, False, 'Event Starts In:'),
            (None, None, True, 'CTF is Paused'),
            (None, now - timedelta(days=1), False, 'CTF has Ended'),
        ):
            with self.subTest(banner=banner):
                with app.app_context():
                    Settings.set('ctf_start_time', start, 'datetime')
                    Settings.set('ctf_end_time', end, 'datetime')
                    Settings.set('ctf_paused', paused, 'bool')
                page = self.player.get('/challenges/').text
                self.assertIn(banner, page)
                self.assertNotIn('Scheduled fixture', page)
                self.assert_unavailable(self.player, target)
                self.assertIn('Scheduled fixture', self.admin.get('/challenges/').text)
                self.assertIn('Scheduled fixture description', self.admin.get(f'/challenges/{target[0]}').text)

    def test_attachment_requires_a_record_and_an_authenticated_viewer(self):
        with app.app_context():
            _, _, filename = self.challenge('Public fixture')
            db.session.commit()
        (self.uploads / 'unregistered.txt').write_text('Unregistered attachment', encoding='utf-8')
        self.assertEqual(app.test_client().get(f'/files/{filename}').status_code, 302)
        self.assertEqual(self.player.get('/files/unregistered.txt').status_code, 404)
        self.assertEqual(self.admin.get('/files/unregistered.txt').status_code, 404)
        with self.player.get(f'/files/{filename}') as attachment:
            self.assertEqual(attachment.data, b'<p>Challenge attachment</p>')

    def test_team_requirements_apply_to_every_challenge_endpoint(self):
        with app.app_context():
            team_only = self.challenge('Team entry fixture', requires_team=True)
            open_entry = self.challenge('Open entry fixture')
            db.session.get(User, 2).team_id = None
            db.session.commit()
        self.assertNotIn('Team entry fixture', self.player.get('/challenges/').text)
        self.assert_unavailable(self.player, team_only)
        self.assertEqual(self.player.get(f'/challenges/{open_entry[0]}').status_code, 200)
        with app.app_context():
            Settings.set('require_team_for_challenges', True, 'bool')
        self.assertEqual(self.player.get('/challenges/').location, '/teams/')
        self.assert_unavailable(self.player, open_entry)
        self.assertEqual(self.admin.get(f'/challenges/{team_only[0]}').status_code, 200)

    def test_container_instance_stays_private_after_owner_leaves_team(self):
        with app.app_context():
            self.container(1)
            db.session.commit()
        self.assertEqual(len(self.player.get('/container/my-containers').json['containers']), 1)
        with app.app_context():
            db.session.get(User, 2).team_id = None
            db.session.commit()
        self.assertEqual(self.player.get('/challenges/1').status_code, 200)
        self.assertEqual(self.player.get('/container/my-containers').json['containers'], [])
        self.assertEqual(self.player.get('/container/status?challenge_id=1').status_code, 404)
        self.assertEqual(self.player.post('/container/stop', headers=self.headers, json={'challenge_id': 1}).status_code, 404)
        self.status.assert_not_called()
        self.stop.assert_not_called()
