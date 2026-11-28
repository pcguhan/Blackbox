from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from models import db
from models.challenge import Challenge
from tests.ui_fixture import app, seed


URL = '/admin/api/versions'


class VersionInventoryTests(unittest.TestCase):
    def setUp(self):
        seed()
        self.client = app.test_client()

    def login(self, user):
        with self.client.session_transaction() as session:
            session.update(_user_id=str(user), _fresh=True)

    def test_only_admins_can_read_versions_or_open_the_dialog(self):
        with patch('routes.admin_versions.collect_versions') as collect:
            self.assertIn(self.client.get(URL).status_code, (302, 401))
            self.login(2)
            self.assertIn(self.client.get(URL).status_code, (302, 403))
            collect.assert_not_called()
        self.assertNotIn(b'versions-dialog', self.client.get('/').data)
        self.assertEqual(self.client.get('/admin/settings').status_code, 302)

    def test_admin_gets_live_inventory_and_can_open_popup(self):
        self.login(1)
        docker = MagicMock()
        docker.version.return_value = {'Version': 'fixture-docker-version'}
        docker.info.return_value = {'OperatingSystem': 'Fixture OS'}
        image = SimpleNamespace(
            tags=['ctf-fixture:1'], attrs={'Config': {
                'Env': ['PYTHON_VERSION=3.11', 'SECRET_TOKEN_VERSION=test-marker'],
            }},
        )
        docker.images.get.return_value = image
        docker.containers.list.return_value = [SimpleNamespace(
            labels={'com.docker.compose.service': 'fixture-service'}, image=image,
        )]
        with app.app_context():
            db.session.get(Challenge, 1).docker_image = 'ctf-fixture:1'
            db.session.commit()
        with patch('services.version_inventory.container_orchestrator') as orchestrator, patch(
            'services.version_inventory._docker_cli_version', return_value='fixture-cli'
        ):
            orchestrator.docker_client = docker
            response = self.client.get(URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        data = response.json
        self.assertTrue({'flask', 'pillow', 'sqlalchemy'} <= {row['name'].lower() for row in data['python_packages']})
        self.assertTrue({'Python', 'Database server'} <= {row['name'] for row in data['runtime']})
        self.assertIn('Bootstrap', {row['name'] for row in data['browser_libraries']})
        self.assertEqual(data['host'][0]['version'], 'fixture-docker-version')
        self.assertEqual(data['services'][0]['name'], 'fixture-service')
        self.assertEqual(data['challenge_images'][0]['image'], 'ctf-fixture:1')
        self.assertEqual(data['challenge_images'][0]['versions'], {'Python': '3.11'})
        self.assertNotIn(b'test-marker', response.data)
        self.assertIn(b'versions-dialog', self.client.get('/admin/settings').data)
