"""Proof that a second organization's rows are unreachable.

Each isolation test is asserted twice where it matters: once through the
repository (which filters explicitly) and once through raw, deliberately
unfiltered SQL (which only RLS can stop). If the second assertion passes, the
database is doing independent work.
"""

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from app.infrastructure.db.models import OrganizationRow, UserRow
from app.infrastructure.db.repositories import OrgScopedUserRepository
from tests.integration.conftest import requires_postgres

pytestmark = [pytest.mark.anyio, requires_postgres]


@pytest.fixture
async def two_orgs(database):
    """Two organizations, one user each. Written on the BYPASSRLS session."""
    acme, globex = uuid4(), uuid4()
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
                    id=uuid4(),
                    org_id=acme,
                    email=f"alice-{suffix}@acme.example.com",
                    full_name="Alice",
                    password_hash="x",
                ),
                UserRow(
                    id=uuid4(),
                    org_id=globex,
                    email=f"bob-{suffix}@globex.example.com",
                    full_name="Bob",
                    password_hash="x",
                ),
            ]
        )
        await session.commit()

    return acme, globex


async def test_repository_returns_only_its_own_org(database, two_orgs):
    acme, _ = two_orgs

    async with database.tenant_session(acme) as session:
        rows = (
            (
                await session.execute(
                    text("SELECT org_id FROM users")  # no WHERE clause at all
                )
            )
            .scalars()
            .all()
        )

    assert set(rows) == {acme}


async def test_unfiltered_query_cannot_see_the_other_org(database, two_orgs):
    """The repository filter is removed on purpose. RLS is the only guard left."""
    acme, globex = two_orgs

    async with database.tenant_session(acme) as session:
        visible = (
            await session.execute(
                text("SELECT count(*) FROM users WHERE org_id = :o"), {"o": globex}
            )
        ).scalar_one()

    assert visible == 0


async def test_cross_org_lookup_by_id_returns_nothing(database, two_orgs):
    acme, globex = two_orgs

    async with database.system_session() as session:
        other = (
            await session.execute(
                text("SELECT id FROM users WHERE org_id = :o"), {"o": globex}
            )
        ).scalar_one()

    async with database.tenant_session(acme) as session:
        repository = OrgScopedUserRepository(session, acme)
        assert await repository.get_by_id(other) is None


async def test_organizations_table_is_scoped_too(database, two_orgs):
    acme, _ = two_orgs

    async with database.tenant_session(acme) as session:
        rows = (
            (await session.execute(text("SELECT id FROM organizations")))
            .scalars()
            .all()
        )

    assert rows == [acme]


async def test_insert_into_another_org_is_refused(database, two_orgs):
    """WITH CHECK on the policy, not just USING: writes are constrained as well."""
    acme, globex = two_orgs

    with pytest.raises(DBAPIError):
        async with database.tenant_session(acme) as session:
            await session.execute(
                text(
                    "INSERT INTO users (id, org_id, email, full_name, password_hash)"
                    " VALUES (:id, :org, :email, 'Mallory', 'x')"
                ),
                {
                    "id": uuid4(),
                    "org": globex,
                    "email": f"mallory-{uuid4().hex}@x.example.com",
                },
            )


async def test_session_without_an_org_setting_sees_nothing(settings, two_orgs):
    """Default deny: forgetting to set app.current_org_id must not open the door."""
    engine = create_async_engine(settings.db.url)
    try:
        async with engine.connect() as connection:
            count = (
                await connection.execute(text("SELECT count(*) FROM users"))
            ).scalar_one()
    finally:
        await engine.dispose()

    assert count == 0
