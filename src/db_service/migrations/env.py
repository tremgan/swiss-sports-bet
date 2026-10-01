"""Alembic environment.

The database URL comes from `config.SQLMODEL_DB_URL` rather than alembic.ini so
that migrations and the running app can never point at different databases.
"""

from logging.config import fileConfig

from alembic import context
from sqlmodel import SQLModel

# Importing the models registers every table on SQLModel.metadata, which is what
# autogenerate diffs the database against.
import core.models  # noqa: F401
from config import SQLMODEL_DB_URL
from sqlalchemy import engine_from_config, pool

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

if not SQLMODEL_DB_URL:
    raise RuntimeError("SQLMODEL_DB_URL must be set to run migrations.")
# set_main_option writes into a ConfigParser, where "%" opens an interpolation
# token. A URL carrying a percent-encoded password — which is exactly what a
# hosted Postgres tells you to use for a password with special characters —
# otherwise raises "invalid interpolation syntax" before anything connects.
# Reading the option back collapses "%%" to "%", so the engine sees the
# original URL.
config.set_main_option("sqlalchemy.url", SQLMODEL_DB_URL.replace("%", "%%"))

target_metadata = SQLModel.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=SQLMODEL_DB_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # SQLite cannot ALTER most things in place; batch mode rewrites the
            # table instead, so the same migration runs on dev and on Postgres.
            render_as_batch=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
