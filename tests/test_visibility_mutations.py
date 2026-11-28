import unittest
from unittest.mock import patch

from models import db
from models.hint import Hint, HintUnlock
from models.notification_read import NotificationRead
from models.user import User
from tests.ui_fixture import app, seed


class VisibilityMutations(unittest.TestCase):
    def setUp(self):
        seed()
        self.admin = self.client_for(1)
        self.player = self.client_for(2)
        self.headers = {'X-CSRFToken': 'visibility-mutations-token'}

    def client_for(self, user_id):
        client = app.test_client()
        with client.session_transaction() as session:
            session['_user_id'] = str(user_id)
            session['_fresh'] = True
            session['_csrf_token'] = 'visibility-mutations-token'
        return client

    def test_hint_unlock_and_prerequisite_belong_to_team(self):
        with app.app_context():
            teammate = User(username='teammate', email='teammate@example.test', password_hash='unused', team_id=1)
            followup = Hint(challenge_id=1, content='Follow-up hint', order=1, requires_hint_id=1)
            db.session.add_all([teammate, followup])
            db.session.flush()
            teammate_id, followup_id = teammate.id, followup.id
            db.session.commit()

        def get_hints(client):
            response = client.get('/hints/challenge/1')
            self.assertEqual(response.status_code, 200)
            return response.json['hints']

        teammate_client = self.client_for(teammate_id)
        self.assertFalse(get_hints(teammate_client)[1]['can_unlock'])
        self.assertEqual(self.player.post('/hints/1/unlock', headers=self.headers).status_code, 200)
        shared, followup = get_hints(teammate_client)
        self.assertTrue(shared['unlocked'])
        self.assertIn('Inspect the response headers.', shared['content'])
        self.assertTrue(followup['can_unlock'])
        self.assertEqual(teammate_client.post('/hints/1/unlock', headers=self.headers).status_code, 400)

        with app.app_context():
            db.session.get(User, 2).team_id = None
            self.assertEqual(HintUnlock.query.one().team_id, 1)
            db.session.commit()
        shared, followup = get_hints(self.player)
        self.assertNotIn('content', shared)
        self.assertFalse(followup['can_unlock'])
        self.assertEqual(self.player.post(f'/hints/{followup_id}/unlock', headers=self.headers).status_code, 400)
        self.assertNotIn('Inspect the response headers.', self.player.get('/challenges/1').text)

    def test_notification_read_is_personal_idempotent_and_deleted_with_notification(self):
        with patch('services.websocket.socketio.emit') as emit:
            created = self.admin.post('/admin/notifications', headers=self.headers, data={
                'title': 'Event notice', 'body': 'Fixture announcement', 'play_sound': 'true',
            })
            self.assertEqual(created.status_code, 302)
            notifications = self.player.get('/api/notifications').json
            notification, = notifications['notifications']
            self.assertEqual(notifications['unread'], 1)
            self.assertTrue(notification['play_sound'])
            path = f"/api/notifications/{notification['id']}"
            self.assertEqual(self.player.post(path + '/delete', headers=self.headers).status_code, 403)
            for _ in range(2):
                self.assertEqual(self.player.post(path + '/read', headers=self.headers).status_code, 200)
            self.assertEqual(self.player.get('/api/notifications').json['unread'], 0)
            self.assertEqual(self.admin.get('/api/notifications').json['unread'], 1)
            with app.app_context():
                self.assertEqual(NotificationRead.query.one().user_id, 2)

            self.assertEqual(self.admin.post(path + '/delete', headers=self.headers).status_code, 200)
            emit.assert_any_call('notification_deleted', {'id': notification['id']}, namespace='/live')
            self.assertEqual(self.player.get('/api/notifications').json['notifications'], [])
            self.assertEqual(self.player.post(path + '/read', headers=self.headers).status_code, 404)
            with app.app_context():
                self.assertEqual(NotificationRead.query.count(), 0)
