"""Install the additive authentication columns on existing databases."""
from sqlalchemy import inspect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.schema import CreateColumn, CreateIndex

from models import db
from models.user import User


def ensure_auth_schema():
    users = User.__table__
    google_sub_index = next(index for index in users.indexes if index.name == 'ix_users_google_sub')

    with db.engine.begin() as connection:
        if not inspect(connection).has_table(users.name):
            return

        def column_exists(name):
            return any(column['name'] == name for column in inspect(connection).get_columns(users.name))

        def index_exists():
            return any(index['name'] == google_sub_index.name and index['unique']
                       for index in inspect(connection).get_indexes(users.name))

        for name in ('google_sub', 'roll_number', 'auth_provider'):
            if column_exists(name):
                continue
            column = CreateColumn(users.c[name]).compile(dialect=connection.dialect)
            try:
                connection.exec_driver_sql(f'ALTER TABLE users ADD COLUMN {column}')
            except SQLAlchemyError:
                if not column_exists(name):
                    raise

        if not index_exists():
            try:
                connection.execute(CreateIndex(google_sub_index))
            except SQLAlchemyError:
                if not index_exists():
                    raise
