"""Proof that the documents table is under the same row-level isolation.

Same discipline as test_tenant_isolation: assertions go through the repository
*and* through deliberately unfiltered SQL, so a passing test means the database
is doing independent work rather than the WHERE clause doing all of it.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from app.domain.document import Document, DocumentStatus
from app.infrastructure.db.models import OrganizationRow, UserRow
from app.infrastructure.db.repositories import OrgScopedDocumentRepository
from tests.integration.conftest import requires_postgres

pytestmark = [pytest.mark.anyio, requires_postgres]


@pytest.fixture
async def two_tenants(database):
    """Two orgs, one user each, written on the BYPASSRLS session."""
    acme, globex = uuid4(), uuid4()
    alice, bob = uuid4(), uuid4()
    suffix = uuid4().hex[:8]

    async with database.system_session() as session:
        session.add_all(
            [
                OrganizationRow(id=acme, name="Acme", slug=f"acme-{suffix}"),
                OrganizationRow(id=globex, name="Globex", slug=f"globex-{suffix}"),
            ]
        )
        await session.flush()
        session.add_all(
            [
                UserRow(
                    id=alice,
                    org_id=acme,
                    email=f"alice-{suffix}@acme.example.com",
                    full_name="Alice",
                    password_hash="x",
                ),
                UserRow(
                    id=bob,
                    org_id=globex,
                    email=f"bob-{suffix}@globex.example.com",
                    full_name="Bob",
                    password_hash="x",
                ),
            ]
        )
        await session.commit()

    return (acme, alice), (globex, bob)


def make_document(org_id, user_id, filename="report.pdf") -> Document:
    document_id = uuid4()
    return Document(
        id=document_id,
        org_id=org_id,
        uploaded_by=user_id,
        filename=filename,
        content_type="application/pdf",
        size_bytes=9,
        sha256="0" * 64,
        storage_key=f"{org_id}/{document_id}",
        status=DocumentStatus.UPLOADED,
        created_at=datetime.now(UTC),
    )


async def _store(database, org_id, user_id, filename="report.pdf") -> Document:
    document = make_document(org_id, user_id, filename)
    async with database.tenant_session(org_id) as session:
        await OrgScopedDocumentRepository(session, org_id).add(document)
    return document


async def test_repository_returns_only_its_own_org(database, two_tenants):
    (acme, alice), (globex, bob) = two_tenants
    await _store(database, acme, alice, "acme.pdf")
    await _store(database, globex, bob, "globex.pdf")

    async with database.tenant_session(acme) as session:
        page = await OrgScopedDocumentRepository(session, acme).list_page(50, None)

    assert [d.filename for d in page.items] == ["acme.pdf"]
    assert page.next_cursor is None


async def test_unfiltered_query_cannot_see_the_other_org(database, two_tenants):
    """The repository filter is removed on purpose. RLS is the only guard left."""
    (acme, alice), (globex, bob) = two_tenants
    await _store(database, acme, alice)
    await _store(database, globex, bob)

    async with database.tenant_session(acme) as session:
        rows = (
            (await session.execute(text("SELECT org_id FROM documents")))
            .scalars()
            .all()
        )

    assert set(rows) == {acme}


async def test_insert_into_another_org_is_refused(database, two_tenants):
    """WITH CHECK on the policy: a write aimed at another tenant is rejected."""
    (acme, _), (globex, bob) = two_tenants
    smuggled = make_document(globex, bob)

    with pytest.raises(DBAPIError):
        async with database.tenant_session(acme) as session:
            # Repository-level org check bypassed deliberately; this asks
            # whether Postgres refuses on its own.
            await session.execute(
                text(
                    "INSERT INTO documents (id, org_id, uploaded_by, filename,"
                    " content_type, size_bytes, sha256, storage_key, status)"
                    " VALUES (:id, :org, :user, 'x.pdf', 'application/pdf', 1,"
                    " :sha, :key, 'uploaded')"
                ),
                {
                    "id": smuggled.id,
                    "org": globex,
                    "user": bob,
                    "sha": smuggled.sha256,
                    "key": smuggled.storage_key,
                },
            )


async def test_repository_refuses_a_document_from_another_org(database, two_tenants):
    (acme, _), (globex, bob) = two_tenants

    async with database.tenant_session(acme) as session:
        repository = OrgScopedDocumentRepository(session, acme)
        with pytest.raises(ValueError, match="does not belong"):
            await repository.add(make_document(globex, bob))


async def test_session_without_an_org_setting_sees_no_documents(
    database, settings, two_tenants
):
    """Default deny: forgetting to set app.current_org_id must not open the door."""
    (acme, alice), _ = two_tenants
    await _store(database, acme, alice)

    engine = create_async_engine(settings.db.url)
    try:
        async with engine.connect() as connection:
            count = (
                await connection.execute(text("SELECT count(*) FROM documents"))
            ).scalar_one()
    finally:
        await engine.dispose()

    assert count == 0
