"""Idempotent development seed: two organizations, one user each.

Runs on the system session (app_auth, BYPASSRLS) because it writes across
tenants, which is exactly what no request-scoped code is allowed to do.

    uv run python -m app.seed
"""

import asyncio
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.infrastructure.db.models import OrganizationRow, UserRow
from app.infrastructure.db.session import Database
from app.infrastructure.security.hashing import Argon2PasswordHasher


@dataclass(frozen=True, slots=True)
class SeedUser:
    email: str
    full_name: str


@dataclass(frozen=True, slots=True)
class SeedOrg:
    name: str
    slug: str
    user: SeedUser


SEED_ORGS = (
    SeedOrg(
        name="Acme Corp",
        slug="acme",
        user=SeedUser(email="alice@acme.example.com", full_name="Alice Martin"),
    ),
    SeedOrg(
        name="Globex",
        slug="globex",
        user=SeedUser(email="bob@globex.example.com", full_name="Bob Dupont"),
    ),
)


async def _seed_org(session: AsyncSession, spec: SeedOrg, password_hash: str) -> None:
    org = await session.scalar(
        select(OrganizationRow).where(OrganizationRow.slug == spec.slug)
    )
    if org is None:
        org = OrganizationRow(name=spec.name, slug=spec.slug)
        session.add(org)
        await session.flush()

    user = await session.scalar(select(UserRow).where(UserRow.email == spec.user.email))
    if user is None:
        session.add(
            UserRow(
                org_id=org.id,
                email=spec.user.email,
                full_name=spec.user.full_name,
                password_hash=password_hash,
                is_active=True,
            )
        )
        await session.flush()


async def seed(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    database = Database(settings)
    # One hash reused for every seeded user: argon2 is slow on purpose, and
    # these are all the same throwaway password anyway.
    password_hash = Argon2PasswordHasher().hash(settings.seed_password)

    try:
        async with database.system_session() as session:
            for spec in SEED_ORGS:
                await _seed_org(session, spec, password_hash)
            await session.commit()
    finally:
        await database.dispose()

    for spec in SEED_ORGS:
        print(f"seeded {spec.slug:8} {spec.user.email}")
    print(f"password for every seeded user: {settings.seed_password}")


if __name__ == "__main__":
    asyncio.run(seed())
