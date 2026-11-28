"""Ensure the scoring column exists on databases created before this release."""
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError

from models import db


def ensure_scoring_schema():
    _ensure_base_points_override()
    ensure_solve_team_switch_constraint()


def ensure_solve_team_switch_constraint():
    """Replace UNIQUE(user_id, challenge_id) with UNIQUE(user_id, team_id, challenge_id).

    The old constraint blocked a user who changed teams from solving a
    challenge for their new team. Idempotent and safe across workers.
    """
    with db.engine.begin() as connection:
        inspector = inspect(connection)
        if not inspector.has_table('solves'):
            return
        names = {item['name'] for item in inspector.get_unique_constraints('solves')}
        names |= {item['name'] for item in inspector.get_indexes('solves') if item.get('unique')}
        steps = []
        if 'unique_user_team_challenge' not in names:
            steps.append('CREATE UNIQUE INDEX unique_user_team_challenge ON solves (user_id, team_id, challenge_id)')
        if 'unique_user_challenge' in names and connection.dialect.name == 'mysql':
            steps.append('ALTER TABLE solves DROP INDEX unique_user_challenge')
        for statement in steps:
            try:
                connection.execute(text(statement))
            except DBAPIError as error:
                # 1061 duplicate key name / 1091 can't drop: another worker got there first.
                if connection.dialect.name != 'mysql' or error.orig.args[0] not in (1061, 1091):
                    raise


def _ensure_base_points_override():
    with db.engine.begin() as connection:
        inspector = inspect(connection)
        if not inspector.has_table('solves'):
            return
        if any(column['name'] == 'base_points_override' for column in inspector.get_columns('solves')):
            return
        try:
            connection.execute(text('ALTER TABLE solves ADD COLUMN base_points_override INT NULL'))
        except DBAPIError as error:
            # Another app worker may have installed the column after inspection.
            if connection.dialect.name != 'mysql' or error.orig.args[0] != 1060:
                raise
