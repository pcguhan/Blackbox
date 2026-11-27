"""Add the nullable profile settings columns to existing installations."""

from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError

from models import db


def ensure_profile_schema():
    with db.engine.connect() as connection:
        inspector = inspect(connection)
        if not inspector.has_table('users'):
            return
        columns = {column['name'] for column in inspector.get_columns('users')}
        for name, length in (('nickname', 80), ('avatar_filename', 64)):
            if name in columns:
                continue
            try:
                connection.execute(text(f'ALTER TABLE users ADD COLUMN {name} VARCHAR({length}) NULL'))
                connection.commit()
            except DBAPIError:
                connection.rollback()
                # Another app worker may have added this column during startup.
                if name not in {column['name'] for column in inspect(connection).get_columns('users')}:
                    raise
