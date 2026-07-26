"""The real PartnerJobSink against a real database.

This is the seam main's stub was written for: resolve partner_job_id, raise
UnknownPartnerJob when nothing is waiting, apply the outcome idempotently.

It needs a real database rather than a fake because the interesting part is
that resolution happens on the BYPASSRLS session - the partner names no tenant,
so there is no org to scope the lookup to until the row is found.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.domain.document import DocumentStatus
from app.domain.errors import UnknownPartnerJob
from app.domain.partner import PartnerJobStatus, PartnerNotification
from app.infrastructure.db.repositories import OrgScopedDocumentRepository
from app.infrastructure.partner_jobs import DbPartnerJobSink
from tests.integration.conftest import requires_postgres
from tests.integration.test_document_isolation import two_tenants  # noqa: F401
from tests.integration.test_pipeline_isolation import add_document

pytestmark = [pytest.mark.anyio, requires_postgres]


def notification(job_id: str, status=PartnerJobStatus.COMPLETED) -> PartnerNotification:
    return PartnerNotification(
        job_id=job_id,
        status=status,
        result={"indexed_at": "2026-05-21T14:23:11Z"},
        occurred_at=datetime.now(UTC),
    )


async def waiting_document(database, org_id, user_id, job_id: str):
    document = await add_document(database, org_id, user_id)
    async with database.tenant_session(org_id) as session:
        await OrgScopedDocumentRepository(session, org_id).await_partner(
            document.id, job_id
        )
    return document


async def status_of(database, org_id, document_id) -> DocumentStatus:
    async with database.tenant_session(org_id) as session:
        stored = await OrgScopedDocumentRepository(session, org_id).get(document_id)
    return stored.status


async def test_a_completed_notification_makes_the_document_ready(database, two_tenants):  # noqa: F811
    (acme, alice), _ = two_tenants
    job_id = f"j_{uuid4().hex[:16]}"
    document = await waiting_document(database, acme, alice, job_id)

    await DbPartnerJobSink(database).deliver(notification(job_id))

    # ready is reachable only through this path - the workflow itself stops at
    # awaiting_partner.
    assert await status_of(database, acme, document.id) is DocumentStatus.READY


async def test_a_failed_notification_fails_the_document(database, two_tenants):  # noqa: F811
    (acme, alice), _ = two_tenants
    job_id = f"j_{uuid4().hex[:16]}"
    document = await waiting_document(database, acme, alice, job_id)

    await DbPartnerJobSink(database).deliver(
        notification(job_id, PartnerJobStatus.FAILED)
    )

    assert await status_of(database, acme, document.id) is DocumentStatus.FAILED


async def test_an_unknown_job_id_is_rejected(database):
    with pytest.raises(UnknownPartnerJob):
        await DbPartnerJobSink(database).deliver(notification("j_never_issued"))


async def test_a_retried_notification_does_not_change_the_outcome(
    database,
    two_tenants,  # noqa: F811
):
    """Partners retry. A stale retry of an earlier failure must not flip a
    document that is already ready."""
    (acme, alice), _ = two_tenants
    job_id = f"j_{uuid4().hex[:16]}"
    document = await waiting_document(database, acme, alice, job_id)
    sink = DbPartnerJobSink(database)

    await sink.deliver(notification(job_id))
    await sink.deliver(notification(job_id, PartnerJobStatus.FAILED))

    assert await status_of(database, acme, document.id) is DocumentStatus.READY
