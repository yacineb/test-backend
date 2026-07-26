"""Engine and session management.

Two pools, because the two roles are not interchangeable:

- `tenant_session` connects as app_rw, which is subject to row-level security.
  It sets app.current_org_id for the life of the transaction, and every policy
  keys off that setting. A query that forgets its WHERE clause returns nothing.
- `system_session` connects as app_auth, which holds BYPASSRLS. It exists for
  the handful of lookups that cannot know an org yet — find a user by email,
  find a refresh token by hash — plus seeding.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings


class Database:
    def __init__(self, settings: Settings) -> None:
        self._rw_engine = self._engine(settings.db.url, settings)
        self._auth_engine = self._engine(settings.db.auth_url, settings)
        self._rw_sessions = async_sessionmaker(self._rw_engine, expire_on_commit=False)
        self._auth_sessions = async_sessionmaker(
            self._auth_engine, expire_on_commit=False
        )

    @staticmethod
    def _engine(url: str, settings: Settings) -> AsyncEngine:
        return create_async_engine(
            url,
            echo=settings.db.echo,
            pool_size=settings.db.pool_size,
            max_overflow=settings.db.max_overflow,
            pool_pre_ping=True,
        )

    @asynccontextmanager
    async def tenant_session(self, org_id: UUID) -> AsyncIterator[AsyncSession]:
        async with self._rw_sessions() as session, session.begin():
            # set_config(..., is_local => true) rather than SET LOCAL because
            # SET does not accept bind parameters. Transaction-scoped, so it
            # cannot leak to the next checkout of this pooled connection.
            await session.execute(
                text("SELECT set_config('app.current_org_id', :org_id, true)"),
                {"org_id": str(org_id)},
            )
            yield session

    @asynccontextmanager
    async def tenant_session_manual(self, org_id: UUID) -> AsyncIterator[AsyncSession]:
        """tenant_session, but the caller decides when to commit.

        The upload path needs this: the document row has to be committed and
        visible before the pipeline that writes to it is enqueued, and
        tenant_session only commits when its block ends.

        Committing clears `app.current_org_id`, because set_config was asked
        for a transaction-local value - deliberately, so it cannot leak to the
        next checkout of a pooled connection. TenantUnitOfWork below puts it
        back after each commit, which keeps that property intact.
        """
        async with self._rw_sessions() as session:
            await self._pin_org(session, org_id)
            yield session

    @staticmethod
    async def _pin_org(session: AsyncSession, org_id: UUID) -> None:
        await session.execute(
            text("SELECT set_config('app.current_org_id', :org_id, true)"),
            {"org_id": str(org_id)},
        )

    @asynccontextmanager
    async def system_session(self) -> AsyncIterator[AsyncSession]:
        # No `session.begin()` wrapper here, unlike tenant_session: the auth use
        # cases commit explicitly through the UnitOfWork port, because replay
        # detection must persist a revocation and then raise. Anything not
        # committed by the time this closes is rolled back.
        async with self._auth_sessions() as session:
            yield session

    async def dispose(self) -> None:
        await self._rw_engine.dispose()
        await self._auth_engine.dispose()


class TenantUnitOfWork:
    """Commits, then re-pins the organization for the next transaction.

    `set_config(..., is_local => true)` is scoped to the transaction, so a
    commit discards it. Anything committing mid-session has to put it back
    before the next statement, or row-level security denies everything.
    Re-pinning rather than switching to a session-level setting keeps the
    guarantee that nothing leaks to the next checkout of a pooled connection.
    """

    def __init__(self, session: AsyncSession, org_id: UUID) -> None:
        self._session = session
        self._org_id = org_id

    async def commit(self) -> None:
        await self._session.commit()
        await Database._pin_org(self._session, self._org_id)
