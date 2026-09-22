"""Alembic environment — URL and SQLite pragmas come from application config."""
from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, event, pool

from kex.config import load_config
from kex.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

app_config = load_config()
config.set_main_option("sqlalchemy.url", app_config.sqlalchemy_url)

target_metadata = Base.metadata


def _install_pragmas(dbapi_conn, _record):  # noqa: ANN001
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA busy_timeout=30000")
    cur.close()


def run_migrations_offline() -> None:
    context.configure(
        url=app_config.sqlalchemy_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = app_config.sqlalchemy_url
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args={"timeout": 30},
    )

    @event.listens_for(connectable, "connect")
    def _pragmas(dbapi_conn, _record):  # noqa: ANN001
        _install_pragmas(dbapi_conn, _record)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
