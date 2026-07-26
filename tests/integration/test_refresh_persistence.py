"""Transactional behaviour of replay detection.

The fakes cannot cover this: revoke_family() is followed immediately by a
raise, and whether the revocation survives depends on a real commit happening
before the session unwinds.
"""

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.application.deps import AuthDeps
from app.application.login import login
from app.application.refresh import refresh
from app.domain.errors import InvalidRefreshToken, RefreshTokenReused
from app.infrastructure.clock import SystemClock
from app.infrastructure.db.models import OrganizationRow, UserRow
from app.infrastructure.db.repositories import (
    SqlAlchemyUnitOfWork,
    UnscopedRefreshTokenRepository,
    UnscopedUserRepository,
)
from app.infrastructure.db.session import Database
from app.infrastructure.security.hashing import Argon2PasswordHasher
from tests.fakes import make_token_service
from tests.integration.conftest import requires_postgres

pytestmark = [pytest.mark.anyio, requires_postgres]

PASSWORD = "password123"


@pytest.fixture
async def database(settings):
    db = Database(settings)
    yield db
    await db.dispose()


@pytest.fixture
async def account(database):
    org_id, suffix = uuid4(), uuid4().hex[:8]
    email = f"alice-{suffix}@acme.example.com"

    async with database.system_session() as session:
        session.add(OrganizationRow(id=org_id, name="Acme", slug=f"acme-{suffix}"))
        await session.flush()
        session.add(
            UserRow(
                id=uuid4(),
                org_id=org_id,
                email=email,
                full_name="Alice",
                password_hash=Argon2PasswordHasher().hash(PASSWORD),
            )
        )
        await session.commit()

    return email


def _deps(session) -> AuthDeps:
    return AuthDeps(
        users=UnscopedUserRepository(session),
        refresh_tokens=UnscopedRefreshTokenRepository(session),
        hasher=Argon2PasswordHasher(),
        tokens=make_token_service(),
        clock=SystemClock(),
        uow=SqlAlchemyUnitOfWork(session),
        refresh_ttl=timedelta(days=30),
    )


async def test_login_and_rotation_persist_across_sessions(database, account):
    async with database.system_session() as session:
        first = await login(_deps(session), account, PASSWORD)

    # A brand new session and connection: nothing is carried over in memory.
    async with database.system_session() as session:
        second = await refresh(_deps(session), first.refresh_token)

    assert second.refresh_token != first.refresh_token


async def test_replay_revocation_survives_the_exception(database, account):
    """Regression guard: the revoking write must be committed, not rolled back."""
    async with database.system_session() as session:
        first = await login(_deps(session), account, PASSWORD)
    async with database.system_session() as session:
        second = await refresh(_deps(session), first.refresh_token)

    async with database.system_session() as session:
        with pytest.raises(RefreshTokenReused):
            await refresh(_deps(session), first.refresh_token)

    # Fresh session: if the revocation had rolled back with the exception, the
    # stolen family would still be usable here.
    async with database.system_session() as session:
        with pytest.raises(InvalidRefreshToken):
            await refresh(_deps(session), second.refresh_token)

    async with database.system_session() as session:
        unrevoked = (
            await session.execute(
                text(
                    "SELECT count(*) FROM refresh_tokens"
                    " WHERE user_id = (SELECT id FROM users WHERE email = :e)"
                    " AND revoked_at IS NULL"
                ),
                {"e": account},
            )
        ).scalar_one()

    assert unrevoked == 0


async def test_failed_login_writes_nothing(database, account):
    from app.domain.errors import InvalidCredentials

    async with database.system_session() as session:
        with pytest.raises(InvalidCredentials):
            await login(_deps(session), account, "wrong-password")

    # Scoped to this account: the schema is created once per session, so other
    # tests' rows are still in the table.
    async with database.system_session() as session:
        count = (
            await session.execute(
                text(
                    "SELECT count(*) FROM refresh_tokens"
                    " WHERE user_id = (SELECT id FROM users WHERE email = :e)"
                ),
                {"e": account},
            )
        ).scalar_one()

    assert count == 0
