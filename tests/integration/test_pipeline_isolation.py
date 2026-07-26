"""Pipeline state under real row-level security.

Same discipline as test_document_isolation: assert through the repository *and*
through deliberately unfiltered SQL, so a pass means the database is doing
independent work.

This matters more for `document_steps` than for `documents`, because pipeline
workers write these rows with **no user present**. Their organization comes
from the workflow argument, which is also what pins `app.current_org_id` — so
if that wiring were wrong, RLS is the thing that would catch it.
"""

from uuid import uuid4

import pytest
from sqlalchemy import text

from app.domain.document import Document, DocumentStatus, Step, StepStatus
from app.infrastructure.db.repositories import OrgScopedDocumentRepository
from tests.integration.conftest import requires_postgres
from tests.integration.test_document_isolation import two_tenants  # noqa: F401

pytestmark = [pytest.mark.anyio, requires_postgres]


def make_document(org_id, user_id) -> Document:
    from datetime import UTC, datetime

    document_id = uuid4()
    return Document(
        id=document_id,
        org_id=org_id,
        uploaded_by=user_id,
        filename="report.pdf",
        content_type="application/pdf",
        size_bytes=9,
        sha256="0" * 64,
        storage_key=f"{org_id}/{document_id}",
        status=DocumentStatus.UPLOADED,
        created_at=datetime.now(UTC),
    )


async def add_document(database, org_id, user_id) -> Document:
    document = make_document(org_id, user_id)
    async with database.tenant_session(org_id) as session:
        await OrgScopedDocumentRepository(session, org_id).add(document)
    return document


async def test_upload_creates_all_four_step_rows(database, two_tenants):  # noqa: F811
    (acme, alice), _ = two_tenants
    document = await add_document(database, acme, alice)

    async with database.tenant_session(acme) as session:
        stored = await OrgScopedDocumentRepository(session, acme).get(document.id)

    # "Not started" is a row, never a missing row.
    assert [step.step for step in stored.steps] == list(Step)
    assert {step.status for step in stored.steps} == {StepStatus.PENDING}


async def test_another_org_cannot_read_the_step_rows(database, two_tenants):  # noqa: F811
    """document_steps carries its own org_id precisely so it can carry its own
    policy; without that this query would return acme's pipeline progress."""
    (acme, alice), (globex, _) = two_tenants
    document = await add_document(database, acme, alice)

    async with database.tenant_session(globex) as session:
        rows = await session.execute(text("SELECT document_id FROM document_steps"))
        assert document.id not in {row[0] for row in rows}


async def test_a_worker_cannot_write_steps_into_another_org(database, two_tenants):  # noqa: F811
    """The policy's WITH CHECK clause, not just its USING clause.

    Stands in for a worker whose session was pinned to the wrong tenant: the
    write is refused rather than silently landing in someone else's rows.
    """
    (acme, alice), (globex, _) = two_tenants
    smuggled = make_document(acme, alice)

    with pytest.raises(Exception):  # noqa: B017 - the driver wraps this variously
        async with database.tenant_session(globex) as session:
            await OrgScopedDocumentRepository(session, acme).add(smuggled)


async def test_the_projection_a_worker_writes_round_trips(database, two_tenants):  # noqa: F811
    """Exactly the sequence a pipeline run performs, through the same
    RLS-scoped path a worker uses."""
    (acme, alice), _ = two_tenants
    document = await add_document(database, acme, alice)

    async with database.tenant_session(acme) as session:
        repository = OrgScopedDocumentRepository(session, acme)
        await repository.start_step(document.id, Step.OCR, attempt=2)
        await repository.finish_step(document.id, Step.OCR, {"chars": 14})
        await repository.await_partner(document.id, f"j_{uuid4().hex[:16]}")

    async with database.tenant_session(acme) as session:
        stored = await OrgScopedDocumentRepository(session, acme).get(document.id)

    assert stored.status is DocumentStatus.AWAITING_PARTNER
    assert stored.partner_job_id is not None
    ocr = next(step for step in stored.steps if step.step is Step.OCR)
    assert ocr.attempts == 2
    assert ocr.output == {"chars": 14}
    assert ocr.started_at is not None and ocr.ended_at is not None


async def test_starting_a_step_moves_the_document_off_uploaded(
    database,
    two_tenants,  # noqa: F811
):
    (acme, alice), _ = two_tenants
    document = await add_document(database, acme, alice)

    async with database.tenant_session(acme) as session:
        await OrgScopedDocumentRepository(session, acme).start_step(
            document.id, Step.OCR, attempt=1
        )

    async with database.tenant_session(acme) as session:
        stored = await OrgScopedDocumentRepository(session, acme).get(document.id)

    assert stored.status is DocumentStatus.PROCESSING
