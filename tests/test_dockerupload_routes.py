import hashlib
import io
import os
import re
import tempfile
import unittest
from pathlib import Path
from functools import partial
from types import SimpleNamespace
from unittest import mock

from tests.ui_fixture import app, seed
from tests.test_dockerupload_builder import DOCKERFILE, saved_image_archive, zip_archive

from config import Config
from models import db
from models.audit_log import AuditLog
from models.branching import ChallengeFlag, ChallengePrerequisite
from models.challenge import Challenge
from models.container import ContainerInstance
from models.file import ChallengeFile
from models.settings import DockerSettings
from models.submission import Solve
from models.user import User
from services import docker_builder
from services.container_manager import container_orchestrator


UPLOAD = '/admin/docker/uploads'


class DockerUploadRouteTests(unittest.TestCase):
    def setUp(self):
        seed()
        self.client = app.test_client()
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(dir=Path.cwd())))
        self.enterContext(mock.patch(
            'routes.docker_upload.TemporaryDirectory',
            side_effect=partial(tempfile.TemporaryDirectory, dir=self.root),
        ))
        self.docker = mock.MagicMock()
        self.docker.api.build.return_value = [{'stream': 'Build complete\n'}]
        self.docker.images.get.return_value.attrs = {'Config': {'ExposedPorts': {'8080/tcp': {}}}}
        self.enterContext(mock.patch.object(docker_builder, '_docker_client', return_value=self.docker))
        start_job = docker_builder.start_job
        self.start_job = self.enterContext(mock.patch.object(
            docker_builder, 'start_job', side_effect=lambda *args, **kwargs: start_job(
                *args, **kwargs, run_inline=True,
            ),
        ))

    def token(self, path=f'{UPLOAD}/new'):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        return re.search(r'name="csrf-token" content="([^"]+)"', response.text)[1]

    def login(self, username='preview-admin'):
        response = self.client.post('/login', data={
            'username': username, 'password': 'PreviewOnly123!', 'csrf_token': self.token('/login'),
        })
        self.assertEqual(response.status_code, 302)

    def archive(self, manifest=b'name: Example\npoints: 100\nunknown_field: true\n'):
        return zip_archive([('Dockerfile', DOCKERFILE), ('challenge.yml', manifest)])

    def post_archive(self, payload, token, filename='example.zip'):
        return self.client.post(UPLOAD, data={'archive': (io.BytesIO(payload), filename), 'csrf_token': token})

    def upload(self, payload=None, filename='example.zip'):
        self.login()
        token = self.token()
        payload = self.archive() if payload is None else payload
        response = self.post_archive(payload, token, filename)
        self.assertEqual(response.status_code, 202, response.text)
        return response.json, f"{UPLOAD}/{response.json['job_id']}", token

    def test_admin_and_csrf_required(self):
        self.assertEqual(self.client.get(f'{UPLOAD}/new').status_code, 302)
        self.login('preview-player')
        self.assertEqual(self.client.get(f'{UPLOAD}/new').status_code, 302)
        self.assertEqual(self.post_archive(self.archive(), 'invalid').status_code, 403)
        self.client = app.test_client()
        self.login()
        self.assertEqual(self.post_archive(self.archive(), 'invalid').status_code, 403)
        self.start_job.assert_not_called()

    def test_build_status_audit_and_ownership(self):
        upload, path, _ = self.upload()
        self.assertEqual(upload['kind'], 'context')
        self.assertEqual(upload['manifest'], {'name': 'Example', 'points': 100})
        self.assertIn('unknown_field', upload['warnings'][0])
        result = self.client.get(path)
        self.assertTrue(result.cache_control.no_store)
        job = result.json
        self.assertEqual(job['status'], 'done')
        self.assertEqual(job['exposed_ports'], ['8080/tcp'])
        self.assertEqual(job['log'], ['Build complete'])
        self.assertEqual(job['sha256'], hashlib.sha256(self.archive()).hexdigest())
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertEqual(self.docker.api.build.call_args.kwargs['tag'], job['image'])
        self.assertEqual(self.client.get(UPLOAD + '/invalid').status_code, 404)
        with app.app_context():
            self.assertEqual(Challenge.query.count(), 1)
            self.assertEqual(AuditLog.query.filter_by(action='DOCKER_IMAGE_UPLOAD', user_id=1).count(), 1)
            other = User(username='other-admin', email='other@example.test', is_admin=True, is_verified=True)
            other.set_password('PreviewOnly123!')
            db.session.add(other)
            db.session.commit()
        for endpoint in ('challenge', 'replace/1', 'test'):
            self.assertEqual(self.client.post(f'{path}/{endpoint}', data={'csrf_token': 'invalid'}).status_code, 403)
        self.client = app.test_client()
        self.login('other-admin')
        self.assertEqual(self.client.get(path).status_code, 404)
        self.assertEqual(self.client.post(path + '/challenge', data={'csrf_token': self.token()}).status_code, 404)

    def test_saved_image_uses_docker_load(self):
        payload = saved_image_archive()
        self.docker.images.load.return_value = [mock.Mock()]
        upload, path, _ = self.upload(payload, 'saved.tar')
        self.assertEqual(upload['kind'], 'image')
        job = self.client.get(path).json
        self.assertEqual(job['status'], 'done')
        self.assertEqual(job['sha256'], hashlib.sha256(payload).hexdigest())
        self.assertTrue(job['image'].startswith('blackbox/saved:'))
        self.docker.images.load.assert_called_once()
        self.docker.api.build.assert_not_called()

    def test_invalid_archives_and_request_limit(self):
        self.login()
        token = self.token()
        for payload in (b'broken archive', self.archive(b'points: false'), zip_archive([('SOLUTION.md', b'private')])):
            with self.subTest(payload=payload[:20]):
                self.assertEqual(self.post_archive(payload, token).status_code, 400)
        with mock.patch.dict(app.config, {'MAX_CONTENT_LENGTH': 1024}):
            self.assertEqual(self.post_archive(b'x' * 1025, token).status_code, 413)
        self.start_job.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_nginx_and_flask_share_the_default_request_limit(self):
        for filename in ('http.conf', 'nginx.conf'):
            config = (Path('conf/nginx') / filename).read_text()
            limits = re.findall(r'client_max_body_size (\d+)M;', config)
            self.assertTrue(limits)
            self.assertEqual({int(limit) * 1024**2 for limit in limits}, {50 * 1024**2})
        if 'MAX_UPLOAD_SIZE' not in os.environ:
            self.assertEqual(Config.MAX_CONTENT_LENGTH, 50 * 1024**2)

    def test_creation_uses_built_tag_and_team_flags(self):
        _, path, token = self.upload()
        tag = self.client.get(path).json['image']
        form = {
            'csrf_token': token, 'name': 'Uploaded challenge', 'category': 'Web', 'description': 'Read the headers.',
            'docker_image': 'ignored:latest', 'flag_mode': 'dynamic', 'flag': 'CYS{ignored}',
            'docker_flag_path': '/flag.txt', 'requires_team': 'true', 'is_visible': 'true',
            'hint_content[]': 'Inspect the response.', 'hint_cost[]': '5',
        }
        for prefix in ('CYS', 'LOCAL'):
            with self.subTest(prefix=prefix):
                form['name'] = f'{prefix} upload'
                if prefix != 'CYS':
                    form['flag_prefix'] = prefix
                result = self.client.post(path + '/challenge', data=form)
                self.assertEqual(result.status_code, 302)
                with app.app_context():
                    challenge = Challenge.query.filter_by(name=form['name']).one()
                    self.assertTrue(challenge.docker_enabled)
                    self.assertTrue(challenge.requires_team)
                    self.assertTrue(challenge.is_visible)
                    self.assertEqual(challenge.docker_image, tag)
                    self.assertEqual(challenge.docker_flag_path, '/flag.txt')
                    self.assertEqual(challenge.hint_objects.one().cost, 5)
                    self.assertTrue(challenge.has_generated_flag())
                    self.assertRegex(challenge.flag, rf'^{prefix}\{{[a-f0-9]{{48}}\}}$')
                    self.assertFalse(challenge.check_flag(challenge.flag, team_id=1, user_id=2))
                    self.assertFalse(challenge.check_flag('CYS{ignored}', team_id=1, user_id=2))

    def test_static_flag_and_form_validation(self):
        _, path, token = self.upload()
        form = {'csrf_token': token, 'name': 'Static upload', 'category': 'Web', 'description': 'Read it.',
                'flag_mode': 'static', 'flag': 'CYS{fixed}', 'initial_points': '100', 'minimum_points': '120'}
        self.assertEqual(self.client.post(path + '/challenge', data=form).status_code, 400)
        form['minimum_points'] = '50'
        self.assertEqual(self.client.post(path + '/challenge', data=form).status_code, 302)
        with app.app_context():
            challenge = Challenge.query.filter_by(name='Static upload').one()
            self.assertEqual(challenge.check_flag('CYS{fixed}', team_id=1, user_id=2).flag_value, 'CYS{fixed}')
            self.assertFalse(challenge.is_visible)

    def test_failed_build_cannot_create_and_cleans_up(self):
        self.docker.api.build.return_value = [{'error': 'Build failed for fixture'}]
        _, path, token = self.upload()
        job = self.client.get(path).json
        self.assertEqual(job['status'], 'error')
        self.assertIn('Build failed for fixture', job['error'])
        self.assertEqual(self.client.post(path + '/challenge', data={'csrf_token': token}).status_code, 409)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_image_requires_an_exposed_port(self):
        self.docker.images.get.return_value.attrs = {'Config': {}}
        _, path, _ = self.upload()
        job = self.client.get(path).json
        self.assertEqual(job['status'], 'error')
        self.assertIn('EXPOSE', job['error'])

    def test_replacement_preserves_team_history_and_branching(self):
        with app.app_context():
            challenge = db.session.get(Challenge, 1)
            challenge.docker_enabled = True
            challenge.docker_image = 'old:latest'
            challenge.requires_team = True
            challenge.is_hidden = True
            challenge.unlock_mode = 'prerequisite'
            challenge.act = 'ACT II'
            next_challenge = Challenge(name='Next', description='Next step', category='Web', flag='CYS{next}')
            db.session.add(next_challenge)
            db.session.flush()
            db.session.add_all([
                DockerSettings(allowed_repositories='old:latest'),
                Solve(challenge_id=1, user_id=2, team_id=1, points_earned=100),
                ChallengePrerequisite(challenge_id=1, prerequisite_challenge_id=next_challenge.id),
                ChallengeFlag(challenge_id=1, flag_value='CYS{old}', unlocks_challenge_id=next_challenge.id),
            ])
            db.session.commit()
        _, path, _ = self.upload()
        token = self.token(f'{UPLOAD}/new?challenge_id=1')
        self.assertEqual(self.client.post(path + '/replace/1', data={'csrf_token': token}).status_code, 400)
        with app.app_context():
            self.assertEqual(db.session.get(Challenge, 1).docker_image, 'old:latest')
        self.assertEqual(self.client.post(path + '/replace/1', data={'csrf_token': token, 'confirm': 'true'}).status_code, 302)
        tag = self.client.get(path).json['image']
        with app.app_context():
            challenge = db.session.get(Challenge, 1)
            self.assertEqual(challenge.docker_image, tag)
            self.assertEqual(challenge.name, 'Inspect the request')
            self.assertEqual(challenge.act, 'ACT II')
            self.assertEqual(challenge.unlock_mode, 'prerequisite')
            self.assertTrue(challenge.requires_team)
            self.assertTrue(challenge.is_solved_by_team(1))
            self.assertEqual(challenge.flags[0].flag_value, 'CYS{old}')
            self.assertEqual(challenge.flags[0].unlocks_challenge_id, 2)
            self.assertEqual(ChallengePrerequisite.query.filter_by(challenge_id=1).count(), 1)
            self.assertEqual(DockerSettings.get_config().get_allowed_repositories_list(), ['old:latest', tag])

    def test_instance_verifies_flag_and_cleans_up(self):
        _, path, token = self.upload()
        container = self.docker.containers.create.return_value
        container.ports = {'8080/tcp': [{'HostPort': '41000'}]}
        container.exec_run.side_effect = lambda *_: SimpleNamespace(
            exit_code=0, output=self.docker.containers.create.call_args.kwargs['environment']['FLAG'].encode(),
        )
        response = self.client.post(path + '/test', data={'csrf_token': token})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {'connection': 'http://127.0.0.1:41000', 'flag_verified': True})
        container.remove.assert_called_once_with(force=True)
        with app.app_context():
            self.assertEqual(ContainerInstance.query.count(), 0)

    def test_author_solution_is_unavailable_by_path_or_original_name(self):
        file = self.root / 'random.md'
        file.write_text('Private solution.')
        with app.app_context():
            db.session.add(ChallengeFile(
                challenge_id=1, original_filename='SOLUTION.md', stored_filename=file.name,
                filepath=str(file), relative_path=file.name,
            ))
            db.session.commit()
        with mock.patch.dict(app.config, {'UPLOAD_FOLDER': str(self.root)}):
            for path in ['/files/random.md', '/files/SolutioN.md', '/static/docker/SOLUTION.md', '/uploads/SOLUTION.md']:
                self.assertEqual(self.client.get(path).status_code, 404)
        self.login()
        self.assertEqual(self.client.post('/admin/challenges/create', data={
            'csrf_token': self.token(), 'files': (io.BytesIO(b'private'), 'SOLUTION.md'),
        }).status_code, 400)
