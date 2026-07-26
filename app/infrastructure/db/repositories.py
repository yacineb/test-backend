"""Repository adapters.

Two flavours, and the split is deliberate rather than a flag on one class:

- `Unscoped*` run on the system session (app_auth, BYPASSRLS) and serve the
  pre-authentication path, where no org is known yet.
- `OrgScoped*` run on the tenant session and additionally filter by org_id in
  the query itself. RLS would already narrow the result; the explicit filter is
  the second layer, so a misconfigured database is not a silent data leak.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.auth import RefreshToken
from app.domain.document import Document, DocumentStatus
from app.domain.organization import Organization
from app.domain.user import User
from app.infrastructure.db.models import (
    DocumentRow,
    OrganizationRow,
    RefreshTokenRow,
    UserRow,
)


def _to_user(row: UserRow) -> User:
    return User(
        id=row.id,
        org_id=row.org_id,
        email=row.email,
        full_name=row.full_name,
        password_hash=row.password_hash,
        is_active=row.is_active,
    )


def _to_organization(row: OrganizationRow) -> Organization:
    return Organization(id=row.id, name=row.name, slug=row.slug)


def _to_document(row: DocumentRow) -> Document:
    return Document(
        id=row.id,
        org_id=row.org_id,
        uploaded_by=row.uploaded_by,
        filename=row.filename,
        content_type=row.content_type,
        size_bytes=row.size_bytes,
        sha256=row.sha256,
        storage_key=row.storage_key,
        status=DocumentStatus(row.status),
        created_at=row.created_at,
    )


def _to_refresh_token(row: RefreshTokenRow) -> RefreshToken:
    return RefreshToken(
        id=row.id,
        user_id=row.user_id,
        org_id=row.org_id,
        family_id=row.family_id,
        token_hash=row.token_hash,
        issued_at=row.issued_at,
        expires_at=row.expires_at,
        consumed_at=row.consumed_at,
        revoked_at=row.revoked_at,
    )


class SqlAlchemyUnitOfWork:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def commit(self) -> None:
        await self._session.commit()


class UnscopedUserRepository:
    """Pre-authentication user lookups. System session only."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_email(self, email: str) -> User | None:
        row = await self._session.scalar(select(UserRow).where(UserRow.email == email))
        return _to_user(row) if row else None

    async def get_by_id(self, user_id: UUID) -> User | None:
        row = await self._session.scalar(select(UserRow).where(UserRow.id == user_id))
        return _to_user(row) if row else None


class OrgScopedUserRepository:
    def __init__(self, session: AsyncSession, org_id: UUID) -> None:
        self._session = session
        self._org_id = org_id

    async def get_by_email(self, email: str) -> User | None:
        row = await self._session.scalar(
            select(UserRow).where(
                UserRow.email == email, UserRow.org_id == self._org_id
            )
        )
        return _to_user(row) if row else None

    async def get_by_id(self, user_id: UUID) -> User | None:
        row = await self._session.scalar(
            select(UserRow).where(UserRow.id == user_id, UserRow.org_id == self._org_id)
        )
        return _to_user(row) if row else None


class OrgScopedOrganizationRepository:
    def __init__(self, session: AsyncSession, org_id: UUID) -> None:
        self._session = session
        self._org_id = org_id

    async def get(self, org_id: UUID) -> Organization | None:
        # Asking for an org other than the caller's own yields nothing.
        row = await self._session.scalar(
            select(OrganizationRow).where(
                OrganizationRow.id == org_id, OrganizationRow.id == self._org_id
            )
        )
        return _to_organization(row) if row else None


class UnscopedRefreshTokenRepository:
    """Refresh tokens are looked up by hash before any org is known."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, token: RefreshToken) -> None:
        self._session.add(
            RefreshTokenRow(
                id=token.id,
                user_id=token.user_id,
                org_id=token.org_id,
                family_id=token.family_id,
                token_hash=token.token_hash,
                issued_at=token.issued_at,
                expires_at=token.expires_at,
                consumed_at=token.consumed_at,
                revoked_at=token.revoked_at,
            )
        )
        await self._session.flush()

    async def get_by_hash(self, token_hash: str) -> RefreshToken | None:
        row = await self._session.scalar(
            select(RefreshTokenRow).where(RefreshTokenRow.token_hash == token_hash)
        )
        return _to_refresh_token(row) if row else None

    async def consume(self, token_id: UUID, now: datetime) -> None:
        await self._session.execute(
            update(RefreshTokenRow)
            .where(
                RefreshTokenRow.id == token_id, RefreshTokenRow.consumed_at.is_(None)
            )
            .values(consumed_at=now)
        )

    async def revoke_family(self, family_id: UUID, now: datetime) -> None:
        await self._session.execute(
            update(RefreshTokenRow)
            .where(
                RefreshTokenRow.family_id == family_id,
                RefreshTokenRow.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )


class OrgScopedDocumentRepository:
    def __init__(self, session: AsyncSession, org_id: UUID) -> None:
        self._session = session
        self._org_id = org_id

    async def add(self, document: Document) -> None:
        if document.org_id != self._org_id:
            # RLS would refuse this too, but that surfaces as an opaque database
            # error. A caller reaching here has a wiring bug, not a bad request.
            raise ValueError("document does not belong to this repository's org")

        self._session.add(
            DocumentRow(
                id=document.id,
                org_id=document.org_id,
                uploaded_by=document.uploaded_by,
                filename=document.filename,
                content_type=document.content_type,
                size_bytes=document.size_bytes,
                sha256=document.sha256,
                storage_key=document.storage_key,
                status=document.status.value,
                created_at=document.created_at,
            )
        )
        # Flush rather than leave it pending: the request transaction commits at
        # teardown, long after the handler could compensate for a failure.
        await self._session.flush()

    async def list_recent(self, limit: int, offset: int) -> list[Document]:
        rows = await self._session.scalars(
            select(DocumentRow)
            .where(DocumentRow.org_id == self._org_id)
            .order_by(DocumentRow.created_at.desc(), DocumentRow.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return [_to_document(row) for row in rows]
