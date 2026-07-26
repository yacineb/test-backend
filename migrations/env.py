import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.infrastructure.db import models  # noqa: F401  (registers the tables)
from app.infrastructure.db.base import Base

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers=False, against the stock Alembic template's
    # default of True. That default sets .disabled on every logger the ini does
    # not name -- including all of app.* -- so anything that runs a migration
    # in-process afterwards logs nothing at all, silently. Harmless in the
    # migrate container, which exits; not harmless anywhere sharing a process.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Migrations run as the owning role, not as app_rw: they create the tables and
# the policies that constrain app_rw.
config.set_main_option("sqlalchemy.url", get_settings().db.migration_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=NullPool,
    )
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
