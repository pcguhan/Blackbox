"""Shared access policy for HTTP and socket standings, including durable freezes."""
import json
from datetime import datetime

from models.settings import Settings
from models.team import Team
from models.user import User
from services.cache import cache_service
from services.scoring import ScoringService


def frozen_snapshot():
    raw = Settings.get('scoreboard_snapshot')
    return json.loads(raw) if raw else None


def team_standings_enabled():
    return Settings.get('teams_enabled', True, type='bool')


def set_frozen(enabled):
    if enabled and not frozen_snapshot():
        teams = ScoringService.get_scoreboard(team_based=True)
        for row in teams:
            row.pop('members', None)
        snapshot = {
            'at': datetime.utcnow().isoformat() + 'Z',
            'teams': teams,
            'individuals': ScoringService.get_scoreboard(team_based=False),
        }
        Settings.set('scoreboard_snapshot', json.dumps(snapshot))
    elif not enabled:
        Settings.set('scoreboard_snapshot', None)


def _frozen_rows(snapshot, teams):
    # Apply current eligibility and roster information to captured scores.
    if teams:
        rosters, admin_teams = ScoringService.team_rosters(User.query.order_by(User.id))
        eligible = {team.id for team in Team.query.filter_by(is_active=True)} - admin_teams
        rows = [dict(row, members=rosters[row['id']])
                for row in snapshot['teams'] if row['id'] in eligible]
    else:
        eligible = {user.id for user in User.query.filter_by(is_active=True, is_admin=False)}
        rows = [dict(row) for row in snapshot['individuals'] if row['id'] in eligible]
    for rank, row in enumerate(rows, 1):
        row['rank'] = rank
    return rows


def _live_rows(teams):
    key = 'scoreboard_full_team' if teams else 'scoreboard_full_individual'
    rows = cache_service.get(key)
    if rows is None:
        rows = ScoringService.get_scoreboard(team_based=teams)
        cache_service.set(key, rows, ttl=60)
    return rows


def standings_for(user):
    if not user.is_authenticated:
        return []
    if not user.is_admin and (not Settings.is_ctf_started() or not Settings.get('scoreboard_visible', True, type='bool')):
        return []
    teams = team_standings_enabled()
    snapshot = frozen_snapshot()
    if snapshot and not user.is_admin:
        return _frozen_rows(snapshot, teams)
    return _live_rows(teams)


def standing_index(rows, user):
    if user.is_admin:
        return None
    entrant_id = user.team_id if team_standings_enabled() else user.id
    return next((index for index, row in enumerate(rows) if row['id'] == entrant_id), None)


def publish_standings():
    from services.websocket import WebSocketService

    WebSocketService.emit_scoreboard_update(_live_rows(team_standings_enabled())[:100])
