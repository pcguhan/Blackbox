from datetime import datetime, timedelta, timezone
from io import BytesIO
import re
import tarfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tests.ui_fixture import app, seed

import gevent
from gevent.event import Event
from docker.errors import APIError, ImageNotFound, NotFound

from models import db
from models.branching import ChallengeFlag, ChallengeUnlock
from models.challenge import Challenge
from models.container import ContainerInstance
from models.flag_abuse import FlagAbuseAttempt
from models.settings import DockerSettings
from models.submission import Solve
from models.team import Team
from models.user import User
from services.cache import cache_service
from services.container_manager import container_orchestrator
from services.container_reconciliation import reconcile_containers
from utils.flag_hmac import generate_hmac_flag


class DockerRuntimeTests(unittest.TestCase):
    def setUp(self):
        seed()
        self.docker = self.enterContext(patch.object(container_orchestrator, 'docker_client', MagicMock()))
        self.enterContext(patch.object(container_orchestrator, '_ensure_docker_client'))
        self.enterContext(patch.dict(app.config, {
            'CHALLENGE_HOST': 'challenges.example.test',
            'CHALLENGE_BIND_ADDRESS': '127.0.0.1',
        }))
        self.containers = {}
        self.docker.containers.run.side_effect = self.run_container
        self.docker.containers.get.side_effect = self.get_container
        self.docker.containers.list.side_effect = lambda **_: list(self.containers.values())
        self.docker.images.get.return_value = SimpleNamespace(
            attrs={'Config': {'ExposedPorts': {'8080/tcp': {}}}},
        )
        with app.app_context():
            first = db.session.get(Challenge, 1)
            first.flag = 'placeholder'
            first.docker_enabled = True
            first.docker_image = 'fixture/challenge:latest'
            first.docker_flag_path = '/run/flags/answer'
            second = Challenge(
                name='Second Docker challenge', description='Fixture', category='Web',
                flag='Custom{placeholder}', docker_enabled=True, docker_image=first.docker_image,
                docker_connection_info='nc {host} {port}', flag_case_sensitive=False,
            )
            captain = db.session.get(User, 2)
            teammate = User(username='teammate', email='teammate@example.test', is_verified=True,
                            team_id=captain.team_id)
            outsider = User(username='outsider', email='outsider@example.test', is_verified=True)
            teammate.set_password('FixtureOnly123!')
            outsider.set_password('FixtureOnly123!')
            db.session.add_all([second, teammate, outsider, DockerSettings(
                allowed_repositories='fixture/', max_containers_per_user=1, max_concurrent_containers=5,
                max_memory_mb=768, max_cpu_percent=37.5, port_range_start=30000, port_range_end=30003,
            )])
            db.session.flush()
            other_team = Team(name='Other Team', invite_code='FIXTURE2', captain_id=outsider.id)
            db.session.add(other_team)
            db.session.flush()
            outsider.team_id = other_team.id
            self.second_id = second.id
            self.team_id = captain.team_id
            self.teammate_id = teammate.id
            self.outsider_id = outsider.id
            self.other_team_id = other_team.id
            db.session.commit()

    def run_container(self, image, **options):
        name = options['name']
        key = next(iter(options['ports']))
        _, port = options['ports'][key]
        container = MagicMock(
            id='id-' + name, short_id='short-id', status='running', labels=options['labels'],
            ports={key: [{'HostPort': str(port)}]}, attrs={},
        )
        container.name = name
        container.put_archive.return_value = True
        container.remove.side_effect = lambda **_: self.containers.pop(name, None)
        self.containers[name] = container
        return container

    def get_container(self, identifier):
        for name, container in self.containers.items():
            if identifier in (name, container.id):
                return container
        raise NotFound('missing')

    def user_client(self, user_id=2):
        client = app.test_client()
        with client.session_transaction() as session:
            session['_user_id'] = str(user_id)
            session['_fresh'] = True
        response = client.get('/profile')
        self.assertEqual(response.status_code, 200)
        csrf = re.search(r'name="csrf-token" content="([^"]+)"', response.text).group(1)
        return client, {'X-CSRF-Token': csrf}

    def action(self, client_info, action='start', challenge_id=1):
        client, headers = client_info
        return client.post(f'/container/{action}', json={'challenge_id': challenge_id}, headers=headers)

    def submit(self, client_info, flag, challenge_id=1, action='submit'):
        client, headers = client_info
        return client.post(f'/challenges/{challenge_id}/{action}', data={'flag': flag}, headers=headers)

    def set_settings(self, **changes):
        with app.app_context():
            settings = DockerSettings.get_config()
            for name, value in changes.items():
                setattr(settings, name, value)
            db.session.commit()

    def start(self, user_id=2, challenge_id=1, team_id=None):
        with app.app_context():
            return container_orchestrator.start_container(
                challenge_id, user_id, '192.0.2.1', self.team_id if team_id is None else team_id,
            )

    def issued_flag(self):
        return self.docker.containers.run.call_args.kwargs['environment']['FLAG']

    def test_env_file_resources_and_separate_public_host(self):
        player = self.user_client()
        response = self.action(player)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json['container']['connection_info'], 'http://challenges.example.test:30000')
        options = self.docker.containers.run.call_args.kwargs
        self.assertEqual(options['ports'], {'8080/tcp': ('127.0.0.1', 30000)})
        self.assertEqual(options['mem_limit'], '768m')
        self.assertEqual(options['nano_cpus'], 375_000_000)
        self.assertEqual(options['pids_limit'], 256)
        flag = self.issued_flag()
        self.assertRegex(flag, r'^CYS\{[a-f0-9]{32}\.[a-f0-9]{64}\}$')
        container = next(iter(self.containers.values()))
        directory, content = container.put_archive.call_args.args
        self.assertEqual(directory, '/run/flags')
        with tarfile.open(fileobj=BytesIO(content)) as archive:
            self.assertEqual(archive.extractfile('answer').read().decode(), flag)
        with patch.dict(app.config, {'CHALLENGE_HOST': 'new.example.test'}):
            client, _ = player
            expected = 'http://new.example.test:30000'
            self.assertEqual(client.get('/container/status?challenge_id=1').json['container']['connection_info'], expected)
            self.assertEqual(client.get('/container/my-containers').json['containers'][0]['connection_info'], expected)

    def test_only_instance_owner_can_solve_and_team_is_credited(self):
        owner = self.user_client()
        teammate = self.user_client(self.teammate_id)
        outsider = self.user_client(self.outsider_id)
        self.assertEqual(self.action(owner).status_code, 200)
        flag = self.issued_flag()
        self.assertEqual(teammate[0].get('/container/status?challenge_id=1').json['status'], 'none')
        for client_info in (teammate, outsider):
            response = self.submit(client_info, flag)
            self.assertEqual(response.status_code, 400, response.json)
            self.assertEqual(response.json['message'], 'Incorrect flag')
        redis = cache_service.redis_client
        self.assertFalse(redis.exists(f'dynamic_flag_mapping:1:team_{self.team_id}'))
        redis.delete(f'solve_lock:1:{self.team_id}')
        with app.app_context(), patch.object(cache_service, 'get', side_effect=AssertionError('flag cache read')):
            self.assertIsNotNone(db.session.get(Challenge, 1).check_flag(flag, self.team_id, 2))
        result = self.submit(owner, flag)
        self.assertEqual(result.status_code, 200, result.json)
        with app.app_context():
            solve = Solve.query.one()
            self.assertEqual((solve.user_id, solve.team_id), (2, self.team_id))
            self.assertEqual(ContainerInstance.query.one().status, 'stopped')
            self.assertEqual(FlagAbuseAttempt.query.filter_by(actual_user_id=2).count(), 2)
        self.assertFalse(self.containers)

    def test_dynamic_only_for_solve_with_explicit_branch_flags_preserved(self):
        self.set_settings(auto_cleanup_on_solve=False)
        owner = self.user_client()
        with app.app_context():
            # A generated seed marks the challenge as per-instance only.
            db.session.get(Challenge, 1).flag = 'CYS{' + 'a' * 48 + '}'
            db.session.add_all([
                ChallengeFlag(challenge_id=1, flag_value='CYS{static}'),
                ChallengeFlag(challenge_id=1, flag_value='CYS{branch}', unlocks_challenge_id=self.second_id),
            ])
            db.session.commit()
        self.assertEqual(self.action(owner).status_code, 200)
        for flag in ('placeholder', 'CYS{static}', 'CYS{branch}'):
            result = self.submit(owner, flag)
            self.assertEqual(result.json['message'], 'Incorrect flag')
            cache_service.redis_client.delete(f'solve_lock:1:{self.team_id}')
        self.assertEqual(self.submit(owner, self.issued_flag()).status_code, 200)
        self.assertTrue(self.containers)
        result = self.submit(owner, 'CYS{branch}', action='explore')
        self.assertEqual(result.status_code, 200, result.json)
        with app.app_context():
            self.assertEqual(Solve.query.count(), 1)
            self.assertEqual(ChallengeUnlock.query.one().team_id, self.team_id)

    def test_custom_prefix_and_case_setting(self):
        owner = self.user_client()
        with patch.dict(app.config, {'CHALLENGE_HOST': ''}):
            response = self.action(owner, challenge_id=self.second_id)
        self.assertEqual(response.json['container']['connection_info'], 'nc localhost 30000')
        flag = self.issued_flag()
        self.assertTrue(flag.startswith('Custom{'))
        result = self.submit(owner, flag.upper(), self.second_id)
        self.assertEqual(result.status_code, 200, result.json)

    def test_revert_rotates_flag_and_keeps_cooldown(self):
        owner = self.user_client()
        self.assertEqual(self.action(owner).status_code, 200)
        first = self.issued_flag()
        result = self.action(owner, 'revert')
        self.assertEqual(result.status_code, 200, result.json)
        second = self.issued_flag()
        self.assertNotEqual(first, second)
        for action in ('revert', 'stop'):
            result = self.action(owner, action)
            self.assertEqual(result.status_code, 400, result.json)
            self.assertGreater(result.json['remaining_seconds'], 0)
        with app.app_context():
            rows = ContainerInstance.query.order_by(ContainerInstance.id).all()
            self.assertEqual(rows[0].last_revert_time, rows[1].last_revert_time)
            challenge = db.session.get(Challenge, 1)
            self.assertIsNone(challenge.check_flag(first, self.team_id, 2))
            self.assertIsNotNone(challenge.check_flag(second, self.team_id, 2))

    def test_legacy_user_flag_remains_scoped_to_its_active_instance(self):
        with app.app_context():
            flag = generate_hmac_flag(1, None, 2)
            instance = ContainerInstance(
                challenge_id=1, user_id=2, team_id=self.team_id, session_id='0123456789abcdef',
                container_id='legacy', container_name='legacy', docker_image='fixture/challenge',
                port=30000, status='running', expires_at=datetime.utcnow() + timedelta(minutes=1),
                dynamic_flag=flag,
            )
            db.session.add(instance)
            db.session.commit()
            challenge = db.session.get(Challenge, 1)
            self.assertIsNotNone(challenge.check_flag(flag, self.team_id, 2))
            self.assertIsNone(challenge.check_flag(flag, self.team_id, self.teammate_id))
            self.assertIsNone(challenge.check_flag(flag, self.other_team_id, 2))
            shared = generate_hmac_flag(1, self.team_id, None)
            instance.dynamic_flag = shared
            db.session.flush()
            self.assertIsNone(challenge.check_flag(shared, self.team_id, 2))
            instance.dynamic_flag = flag
            instance.expires_at = datetime.utcnow()
            db.session.commit()
            self.assertIsNone(challenge.check_flag(flag, self.team_id, 2))

    def test_user_cap_counts_starting_and_global_cap_serializes_teammates(self):
        self.set_settings(max_concurrent_containers=1)
        entered, release = Event(), Event()

        def paused_run(image, **options):
            entered.set()
            if not release.wait(timeout=5):
                raise RuntimeError('timed out waiting to release Docker start')
            return self.run_container(image, **options)

        self.docker.containers.run.side_effect = paused_run
        first = gevent.spawn(self.start)
        try:
            self.assertTrue(entered.wait(timeout=5))
            second = gevent.spawn(self.start, self.teammate_id)
            gevent.sleep(0)
            self.assertFalse(second.ready())
        finally:
            release.set()
        self.assertTrue(first.get(timeout=5)['success'])
        self.assertIn('server has reached', second.get(timeout=5)['error'])
        self.docker.containers.run.assert_called_once()
        with app.app_context():
            ContainerInstance.query.one().status = 'starting'
            db.session.commit()
        result = self.start(challenge_id=self.second_id)
        self.assertEqual(result['existing_challenge_id'], 1)
        self.assertIn('your active instance limit', result['error'])

    def test_port_scan_skips_published_ports_and_retries_bind_collision(self):
        self.containers['other'] = SimpleNamespace(id='other', ports={'80/tcp': [{'HostPort': '30000'}]})
        self.docker.containers.run.side_effect = self.bind_collision_then_start
        result = self.start()
        self.assertTrue(result['success'], result)
        self.assertEqual(result['container']['host_port'], 30002)
        self.set_settings(port_range_end=30000)
        result = self.start(self.teammate_id)
        self.assertFalse(result['success'])
        self.assertIn('No free ports', result['error'])

    def bind_collision_then_start(self, image, **options):
        if next(iter(options['ports'].values()))[1] == 30001:
            self.run_container(image, **options)
            raise APIError('port is already allocated')
        return self.run_container(image, **options)

    def test_failed_injection_releases_port_and_removes_partial_container(self):
        with patch.object(container_orchestrator, '_inject_flag_into_container', return_value=False):
            with self.assertLogs(app.logger, level='ERROR'):
                result = self.start()
        self.assertFalse(result['success'])
        self.assertFalse(self.containers)
        with app.app_context():
            self.assertEqual(ContainerInstance.query.one().status, 'error')
        self.assertTrue(self.start()['success'])

    def test_image_pull_reuses_instance_and_unset_limits_are_omitted(self):
        self.set_settings(max_cpu_percent=0, max_memory_mb=None)
        self.docker.containers.run.side_effect = self.missing_image_then_start
        self.assertTrue(self.start()['success'])
        self.docker.images.pull.assert_called_once_with('fixture/challenge:latest')
        first, retry = self.docker.containers.run.call_args_list
        self.assertEqual(first, retry)
        self.assertNotIn('mem_limit', retry.kwargs)
        self.assertNotIn('nano_cpus', retry.kwargs)
        with app.app_context():
            self.assertEqual(ContainerInstance.query.count(), 1)

    def missing_image_then_start(self, image, **options):
        if self.docker.containers.run.call_count == 1:
            raise ImageNotFound('missing image')
        return self.run_container(image, **options)

    def test_reconciliation_handles_missing_exited_expired_and_inactive_rows(self):
        for user_id, team_id in ((2, self.team_id), (self.teammate_id, self.team_id),
                                 (self.outsider_id, self.other_team_id), (1, self.team_id)):
            self.assertTrue(self.start(user_id, team_id=team_id)['success'])
        with app.app_context():
            rows = ContainerInstance.query.order_by(ContainerInstance.id).all()
            self.containers.pop(rows[0].container_name)
            self.containers[rows[1].container_name].status = 'exited'
            rows[2].expires_at = datetime.utcnow() - timedelta(seconds=1)
            rows[3].status = 'stopped'
            db.session.commit()
        self.set_settings(auto_cleanup_expired=False, cleanup_stale_containers=False)
        reconcile_containers(app)
        with app.app_context():
            rows = ContainerInstance.query.order_by(ContainerInstance.id).all()
            self.assertEqual([row.status for row in rows], ['stopped', 'stopped', 'running', 'stopped'])
        self.assertEqual(len(self.containers), 2)
        self.set_settings(auto_cleanup_expired=True, cleanup_stale_containers=True)
        reconcile_containers(app)
        self.assertFalse(self.containers)
        with app.app_context():
            self.assertTrue(all(row.status == 'stopped' for row in ContainerInstance.query))
            self.assertIn('expired', ContainerInstance.query.filter_by(user_id=self.outsider_id).one().error_message)

    def test_failed_remove_retains_reservation_for_retry(self):
        owner = self.user_client()
        self.assertEqual(self.action(owner).status_code, 200)
        container = next(iter(self.containers.values()))
        remove = container.remove.side_effect
        container.remove.side_effect = APIError('cannot remove')
        with self.assertLogs(app.logger, level='ERROR'):
            self.assertEqual(self.action(owner, 'revert').status_code, 400)
        with app.app_context():
            instance = ContainerInstance.query.one()
            self.assertEqual(instance.status, 'running')
            instance.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
            db.session.commit()
        with self.assertLogs('services.container_reconciliation', level='ERROR'):
            reconcile_containers(app)
        container.remove.side_effect = remove
        self.assertEqual(self.action(owner, 'force-cleanup').status_code, 200)
        with app.app_context():
            self.assertEqual(ContainerInstance.query.one().status, 'stopped')


if __name__ == '__main__':
    unittest.main()
