import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.config import get_settings
from app.db import Base
# import models so metadata is populated
from app import models  # noqa: F401
config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().database_url)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=get_settings().database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """Support invocation standalone (CLI) and inside a running loop (tests)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(run_async_migrations())
    else:
        # Called from within an event loop (e.g. pytest-asyncio): execute the
        # coroutine on a fresh loop in another thread to avoid nested loops.
        import threading

        result: dict = {}

        def _runner():
            new_loop = asyncio.new_event_loop()
            try:
                result["value"] = new_loop.run_until_complete(
                    run_async_migrations()
                )
            except BaseException as exc:  # propagate to caller
                result["error"] = exc
            finally:
                new_loop.close()

        thread = threading.Thread(target=_runner)
        thread.start()
        thread.join()
        if "error" in result:
            raise result["error"]


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
