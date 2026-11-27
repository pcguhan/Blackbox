"""Ensure the scoring column exists on databases created before this release."""
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError

from models import db


def ensure_scoring_schema():
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
