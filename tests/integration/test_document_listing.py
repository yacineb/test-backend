"""Keyset paging against real Postgres.

The fake in tests/fakes.py can prove the API contract but not the two things
that only the database decides: that `(created_at, id) < (:a, :b)` means what
row-value comparison means rather than what a hand-written disjunction would
mean, and that the join to `users` returns the uploader through RLS.

Ties on `created_at` are the whole point, so they are inserted deliberately
rather than hoped for.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.api.cursors import decode_cursor, encode_cursor
from app.domain.document import Document, DocumentCursor, DocumentStatus
from app.infrastructure.db.models import OrganizationRow, UserRow
from app.infrastructure.db.repositories import OrgScopedDocumentRepository
from tests.integration.conftest import requires_postgres

pytestmark = [pytest.mark.anyio, requires_postgres]

NOW = datetime(2026, 7, 26, 10, 0, tzinfo=UTC)


@pytest.fixture
async def org_with_two_users(database):
    """One org, two users, so the join has something to distinguish."""
    org, alice, bob = uuid4(), uuid4(), uuid4()
    suffix = uuid4().hex[:8]

    async with database.system_session() as session:
        session.add(OrganizationRow(id=org, name="Acme", slug=f"acme-{suffix}"))
        await session.flush()
        session.add_all(
            [
                UserRow(
                    id=alice,
                    org_id=org,
                    email=f"alice-{suffix}@acme.example.com",
                    full_name="Alice Martin",
                    password_hash="x",
                ),
                UserRow(
                    id=bob,
                    org_id=org,
                    email=f"bob-{suffix}@acme.example.com",
                    full_name="Bob Dupont",
                    password_hash="x",
                ),
            ]
        )
        await session.commit()

    return org, alice, bob


async def store(database, org, user, filename, created_at) -> Document:
    document_id = uuid4()
    document = Document(
        id=document_id,
        org_id=org,
        uploaded_by=user,
        filename=filename,
        content_type="application/pdf",
        size_bytes=9,
        sha256="0" * 64,
        storage_key=f"{org}/{document_id}",
        status=DocumentStatus.UPLOADED,
        created_at=created_at,
    )
    async with database.tenant_session(org) as session:
        await OrgScopedDocumentRepository(session, org).add(document)
    return document


async def walk(database, org, limit: int) -> list[str]:
    """Page all the way through, through the encoded cursor a client would use."""
    seen: list[str] = []
    cursor = None
    for _ in range(50):  # Bounded: a cursor that stops advancing fails, not hangs.
        async with database.tenant_session(org) as session:
            page = await OrgScopedDocumentRepository(session, org).list_page(
                limit, decode_cursor(cursor) if cursor else None
            )
        seen += [item.filename for item in page.items]
        if page.next_cursor is None:
            return seen
        cursor = encode_cursor(page.next_cursor)
    raise AssertionError("cursor never terminated")


async def test_the_uploader_comes_from_the_join(database, org_with_two_users):
    org, alice, bob = org_with_two_users
    await store(database, org, alice, "alice.pdf", NOW)
    await store(database, org, bob, "bob.pdf", NOW + timedelta(seconds=1))

    async with database.tenant_session(org) as session:
        page = await OrgScopedDocumentRepository(session, org).list_page(50, None)

    by_name = {item.filename: item for item in page.items}
    assert by_name["alice.pdf"].uploader_id == alice
    assert by_name["alice.pdf"].uploader_name == "Alice Martin"
    assert by_name["alice.pdf"].uploader_email.startswith("alice-")
    assert by_name["bob.pdf"].uploader_name == "Bob Dupont"


async def test_documents_come_back_newest_first(database, org_with_two_users):
    org, alice, _ = org_with_two_users
    for n in range(5):
        await store(database, org, alice, f"doc{n}.pdf", NOW + timedelta(seconds=n))

    async with database.tenant_session(org) as session:
        page = await OrgScopedDocumentRepository(session, org).list_page(50, None)

    assert [item.filename for item in page.items] == [
        f"doc{n}.pdf" for n in reversed(range(5))
    ]


async def test_paging_over_a_shared_timestamp_loses_nothing(
    database, org_with_two_users
):
    """Nine documents, one timestamp. Only the id half of the key separates
    them, and a page boundary is forced to land inside the tie."""
    org, alice, _ = org_with_two_users
    for n in range(9):
        await store(database, org, alice, f"tie{n}.pdf", NOW)

    seen = await walk(database, org, limit=2)

    assert sorted(seen) == sorted(f"tie{n}.pdf" for n in range(9))
    assert len(seen) == len(set(seen))


async def test_paging_is_stable_when_a_document_is_added_mid_scroll(
    database, org_with_two_users
):
    """The reason this is not OFFSET: a new document at the head would shift
    every later page by one, hiding a row and repeating another."""
    org, alice, _ = org_with_two_users
    for n in range(4):
        await store(database, org, alice, f"doc{n}.pdf", NOW + timedelta(seconds=n))

    async with database.tenant_session(org) as session:
        first = await OrgScopedDocumentRepository(session, org).list_page(2, None)
    assert [item.filename for item in first.items] == ["doc3.pdf", "doc2.pdf"]

    await store(database, org, alice, "newest.pdf", NOW + timedelta(seconds=99))

    async with database.tenant_session(org) as session:
        rest = await OrgScopedDocumentRepository(session, org).list_page(
            50, first.next_cursor
        )

    # doc1 is not skipped and doc2 is not repeated. The insert is simply not on
    # this page, because the page is defined by a position, not by a count.
    assert [item.filename for item in rest.items] == ["doc1.pdf", "doc0.pdf"]


async def test_a_full_last_page_reports_no_next_cursor(database, org_with_two_users):
    org, alice, _ = org_with_two_users
    for n in range(4):
        await store(database, org, alice, f"doc{n}.pdf", NOW + timedelta(seconds=n))

    async with database.tenant_session(org) as session:
        page = await OrgScopedDocumentRepository(session, org).list_page(4, None)

    assert len(page.items) == 4
    assert page.next_cursor is None


async def test_a_cursor_past_the_end_yields_an_empty_final_page(
    database, org_with_two_users
):
    org, alice, _ = org_with_two_users
    await store(database, org, alice, "only.pdf", NOW)

    async with database.tenant_session(org) as session:
        repository = OrgScopedDocumentRepository(session, org)
        page = await repository.list_page(1, None)
        assert page.next_cursor is None
        # A client that hoarded a cursor from a longer listing gets nothing,
        # not an error and not a wrapped-around first page.
        last = page.items[0]
        beyond = await repository.list_page(
            1, DocumentCursor(created_at=last.created_at, id=last.id)
        )

    assert beyond.items == []
    assert beyond.next_cursor is None
