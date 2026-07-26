"""Integration fixtures. These need a real Postgres — RLS has no SQLite analogue.

Opt in by pointing TEST_POSTGRES_DSN at a superuser connection, e.g.

    docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=postgres \\
        -e POSTGRES_DB=appdb --name pg-test postgres:17-alpine
    TEST_POSTGRES_DSN=postgres:postgres@localhost:5433/appdb uv run pytest

Without it the whole directory is skipped, so `uv run pytest` stays green on a
machine with no Docker.
"""

import asyncio
import os
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import Settings, get_settings
from app.infrastructure.db.session import Database

DSN = os.environ.get("TEST_POSTGRES_DSN")

requires_postgres = pytest.mark.skipif(
    DSN is None, reason="set TEST_POSTGRES_DSN to run integration tests"
)


def _url(credentials: str) -> str:
    # DSN is "user:password@host:port/dbname"; swap in the role we want.
    _, _, location = DSN.partition("@")
    return f"postgresql+asyncpg://{credentials}@{location}"


async def _reset_schema() -> None:
    engine = create_async_engine(
        _url(DSN.partition("@")[0]), isolation_level="AUTOCOMMIT"
    )
    async with engine.connect() as connection:
        await connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        await connection.execute(text("CREATE SCHEMA public"))
    await engine.dispose()


@pytest.fixture
async def database(settings: Settings):
    """A Database bound to the throwaway Postgres, disposed after each test."""
    db = Database(settings)
    yield db
    await db.dispose()


@pytest.fixture(scope="session")
def settings() -> Iterator[Settings]:
    if DSN is None:
        pytest.skip("TEST_POSTGRES_DSN is not set")

    os.environ["MIGRATION_DATABASE_URL"] = _url(DSN.partition("@")[0])
    os.environ["DATABASE_URL"] = _url("app_rw:app_rw")
    os.environ["AUTH_DATABASE_URL"] = _url("app_auth:app_auth")
    get_settings.cache_clear()

    asyncio.run(_reset_schema())
    # Run the real migration rather than metadata.create_all: the policies and
    # the roles only exist there, and this way the migration is under test too.
    command.upgrade(Config("alembic.ini"), "head")

    yield get_settings()

    get_settings.cache_clear()
