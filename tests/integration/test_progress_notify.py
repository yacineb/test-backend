"""Proof that the database itself announces a status change.

This is the test worth having. The hub and the endpoint are exercised with
fakes elsewhere; what cannot be faked is whether migration 0004's trigger
actually fires, on both tables, with the right payload, and only on commit.
If this passes, the push half of progress tracking is real.
"""

import asyncio
from uuid import uuid4

import asyncpg
import pytest

from app.domain.document import Step
from app.infrastructure.db.repositories import OrgScopedDocumentRepository
from app.infrastructure.progress import asyncpg_dsn
from tests.integration.conftest import requires_postgres
from tests.integration.test_document_isolation import two_tenants  # noqa: F401
from tests.integration.test_pipeline_isolation import add_document

pytestmark = [pytest.mark.anyio, requires_postgres]

CHANNEL = "document_progress"


class Notifications:
    """Collects payloads delivered on the channel."""

    def __init__(self) -> None:
        self.received: list[str] = []
        self._arrived = asyncio.Event()

    def __call__(self, _connection, _pid, _channel, payload: str) -> None:
        self.received.append(payload)
        self._arrived.set()

    async def wait(self, timeout: float = 5.0) -> None:
        await asyncio.wait_for(self._arrived.wait(), timeout)
        self._arrived.clear()


@pytest.fixture
async def notifications(settings):
    """A real LISTEN connection, as an API process would hold."""
    connection = await asyncpg.connect(asyncpg_dsn(settings.db.url))
    sink = Notifications()
    await connection.add_listener(CHANNEL, sink)
    try:
        yield sink
    finally:
        await connection.remove_listener(CHANNEL, sink)
        await connection.close()


async def test_inserting_a_document_notifies(database, two_tenants, notifications):  # noqa: F811
    (acme, alice), _ = two_tenants

    document = await add_document(database, acme, alice)
    await notifications.wait()

    assert str(document.id) in notifications.received


async def test_a_step_transition_notifies_with_the_document_id(
    database,
    two_tenants,  # noqa: F811
    notifications,
):
    """The step row spells the id `document_id`, and the trigger is told which
    column to read. A subscriber keys on the document, not on the step."""
    (acme, alice), _ = two_tenants
    document = await add_document(database, acme, alice)
    await notifications.wait()
    notifications.received.clear()

    async with database.tenant_session(acme) as session:
        await OrgScopedDocumentRepository(session, acme).start_step(
            document.id, Step.OCR, attempt=1
        )
    await notifications.wait()

    assert str(document.id) in notifications.received


async def test_nothing_is_announced_before_the_commit(
    database,
    two_tenants,  # noqa: F811
    notifications,
):
    """NOTIFY is transactional. A listener is never told about a row it could
    not yet read - which is what lets subscribers re-read blindly on a wake."""
    (acme, alice), _ = two_tenants
    document = await add_document(database, acme, alice)
    await notifications.wait()
    notifications.received.clear()

    async with database.tenant_session(acme) as session:
        repository = OrgScopedDocumentRepository(session, acme)
        await repository.start_step(document.id, Step.OCR, attempt=1)
        # Still inside the transaction.
        await asyncio.sleep(0.2)
        assert notifications.received == []

    # Committed on block exit.
    await notifications.wait()
    assert str(document.id) in notifications.received


async def test_a_rolled_back_change_is_never_announced(
    database,
    two_tenants,  # noqa: F811
    notifications,
):
    (acme, alice), _ = two_tenants
    document = await add_document(database, acme, alice)
    await notifications.wait()
    notifications.received.clear()

    with pytest.raises(RuntimeError):
        async with database.tenant_session(acme) as session:
            await OrgScopedDocumentRepository(session, acme).start_step(
                document.id, Step.OCR, attempt=1
            )
            raise RuntimeError("something went wrong after the write")

    await asyncio.sleep(0.3)
    assert notifications.received == []


async def test_the_webhook_path_also_notifies(
    database,
    two_tenants,  # noqa: F811
    notifications,
):
    """The reason this is a trigger rather than application code.

    `ready` is set by the partner sink through a different repository on the
    system session. Application-level notification is exactly what forgets a
    path like this; the trigger cannot.
    """
    from app.infrastructure.db.repositories import UnscopedDocumentRepository

    (acme, alice), _ = two_tenants
    document = await add_document(database, acme, alice)
    job_id = f"j_{uuid4().hex[:16]}"
    async with database.tenant_session(acme) as session:
        await OrgScopedDocumentRepository(session, acme).await_partner(
            document.id, job_id
        )
    await notifications.wait()
    notifications.received.clear()

    async with database.system_session() as session:
        await UnscopedDocumentRepository(session).complete(document.id)
        await session.commit()
    await notifications.wait()

    assert str(document.id) in notifications.received
