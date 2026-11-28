"""Hidden challenge names must not leak through other teams' pages or live solve broadcasts."""
import unittest
from unittest.mock import patch

from tests.ui_fixture import app, seed
from models import db
from models.challenge import Challenge
from models.submission import Solve
from models.team import Team
from models.user import User


class HiddenChallengeNames(unittest.TestCase):
    def setUp(self):
        seed()
        with app.app_context():
            outsider = User(username='outsider', email='outsider@example.test', is_verified=True)
            outsider.set_password('PreviewOnly123!')
            db.session.add(outsider)
            db.session.flush()
            other = Team(name='Other team', invite_code='OTHER123', captain_id=outsider.id)
            db.session.add(other)
            db.session.flush()
            outsider.team_id = other.id
            hidden = Challenge(name='Secret branch fixture', description='hidden', category='Web',
                               flag='CYS{secret}', is_hidden=True, is_visible=False, unlock_mode='flag_unlock')
            db.session.add(hidden)
            db.session.flush()
            db.session.add(Solve(user_id=2, team_id=1, challenge_id=hidden.id, points_earned=50))
            db.session.commit()
            self.outsider_id = outsider.id

    def client_for(self, user_id):
        client = app.test_client()
        with client.session_transaction() as session:
            session.update(_user_id=str(user_id), _fresh=True)
        return client

    def test_other_team_cannot_see_hidden_solve_on_team_page(self):
        outsider_view = self.client_for(self.outsider_id).get('/teams/1').get_data(as_text=True)
        self.assertNotIn('Secret branch fixture', outsider_view)
        member_view = self.client_for(2).get('/teams/1').get_data(as_text=True)
        self.assertIn('Secret branch fixture', member_view)

    def test_live_solve_broadcast_redacts_hidden_challenge(self):
        from models.branching import ChallengeUnlock

        with app.app_context():
            branch = Challenge(name='Unlocked branch fixture', description='branch', category='Crypto',
                               flag='CYS{branch}', is_hidden=True, is_visible=False, unlock_mode='flag_unlock')
            db.session.add(branch)
            db.session.flush()
            from models.branching import ChallengeFlag
            source_flag = ChallengeFlag(challenge_id=1, flag_value='CYS{source}', unlocks_challenge_id=branch.id)
            db.session.add(source_flag)
            db.session.flush()
            db.session.add(ChallengeUnlock(challenge_id=branch.id, team_id=1, user_id=2,
                                           unlocked_by_flag_id=source_flag.id))
            db.session.commit()
            branch_id = branch.id
        player = self.client_for(2)
        with player.session_transaction() as session:
            token = session.setdefault('_csrf_token', 'fixture-csrf')
        with patch('routes.challenges.WebSocketService.emit_new_solve') as emit:
            response = player.post(f'/challenges/{branch_id}/submit', data={'flag': 'CYS{branch}'},
                                   headers={'X-CSRF-Token': token})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        payload = emit.call_args.args[0]
        self.assertEqual(payload['challenge'], 'Hidden challenge')
        self.assertIsNone(payload['category'])


if __name__ == '__main__':
    unittest.main()
