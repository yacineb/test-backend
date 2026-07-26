"""Worker-side wiring.

DBOS workflows are module-level functions with no request behind them, so the
database arrives here at startup instead of through a FastAPI dependency. This
is the one piece of process-global state in the pipeline, and it exists because
the orchestrator's programming model requires it.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from app.infrastructure.db.repositories import OrgScopedDocumentRepository
from app.infrastructure.db.session import Database


class TenantDocumentTransaction:
    """Opens one RLS-scoped transaction per call, for a fixed organization.

    A class rather than a closure so it holds two fields instead of an entire
    enclosing scope - these instances outlive the request that made them when
    the pipeline is the caller.
    """

    def __init__(self, database: Database, org_id: UUID) -> None:
        self._database = database
        self._org_id = org_id

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[OrgScopedDocumentRepository]:
        async with self._database.tenant_session(self._org_id) as session:
            yield OrgScopedDocumentRepository(session, self._org_id)


_database: Database | None = None


def bind(database: Database) -> None:
    """Give the workers the same connection pool the API uses."""
    global _database
    _database = database


def transaction_for(org_id: UUID) -> TenantDocumentTransaction:
    """Pipeline writes go through RLS like everything else.

    The org comes from the workflow's own argument, which is also what pins the
    session's `app.current_org_id`. A worker cannot write outside the tenant it
    was started for, even though no user is present.
    """
    if _database is None:  # pragma: no cover - a startup-order mistake
        raise RuntimeError("pipeline runtime is not bound; call bind() at startup")
    return TenantDocumentTransaction(_database, org_id)
