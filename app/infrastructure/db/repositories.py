"""Repository adapters.

Two flavours, and the split is deliberate rather than a flag on one class:

- `Unscoped*` run on the system session (app_auth, BYPASSRLS) and serve the
  pre-authentication path, where no org is known yet.
- `OrgScoped*` run on the tenant session and additionally filter by org_id in
  the query itself. RLS would already narrow the result; the explicit filter is
  the second layer, so a misconfigured database is not a silent data leak.
"""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import Row, func, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.auth import RefreshToken
from app.domain.document import (
    STEPS,
    Document,
    DocumentCursor,
    DocumentPage,
    DocumentStatus,
    DocumentStep,
    DocumentSummary,
    Step,
    StepStatus,
)
from app.domain.organization import Organization
from app.domain.user import User
from app.infrastructure.db.models import (
    DocumentRow,
    DocumentStepRow,
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


def _to_step(row: DocumentStepRow) -> DocumentStep:
    return DocumentStep(
        step=Step(row.step),
        status=StepStatus(row.status),
        attempts=row.attempts,
        last_error=row.last_error,
        output=row.output,
        started_at=row.started_at,
        ended_at=row.ended_at,
    )


def _to_document(row: DocumentRow, steps: Sequence[DocumentStepRow] = ()) -> Document:
    order = {step: index for index, step in enumerate(STEPS)}
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
        workflow_id=row.workflow_id,
        partner_job_id=row.partner_job_id,
        partner_result=row.partner_result,
        partner_occurred_at=row.partner_occurred_at,
        failed_step=Step(row.failed_step) if row.failed_step else None,
        steps=tuple(sorted((_to_step(s) for s in steps), key=lambda s: order[s.step])),
    )


def _to_summary(row: Row) -> DocumentSummary:
    return DocumentSummary(
        id=row.id,
        filename=row.filename,
        status=DocumentStatus(row.status),
        created_at=row.created_at,
        uploader_id=row.uploader_id,
        uploader_name=row.uploader_name,
        uploader_email=row.uploader_email,
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
        # All four step rows up front, in the same transaction as the document.
        # "Not started" is then a row with status=pending, and every later
        # writer can assume its row exists.
        self._session.add_all(
            DocumentStepRow(
                document_id=document.id,
                step=step,
                org_id=self._org_id,
                status=StepStatus.PENDING,
                attempts=0,
            )
            for step in STEPS
        )
        # Flush rather than leave it pending: the request transaction commits at
        # teardown, long after the handler could compensate for a failure.
        await self._session.flush()

    async def list_page(self, limit: int, after: DocumentCursor | None) -> DocumentPage:
        """Keyset page over (created_at DESC, id DESC).

        The join is inner, and safe as one: `uploaded_by` is a NOT NULL foreign
        key to `users`, and it is only ever written from the same access token
        that supplies `org_id`, so the uploader is always a visible row of the
        caller's own organization. If that ever stops holding, this drops
        documents rather than showing them with a blank uploader — which is why
        the org predicate below is on both sides and not left to RLS alone.
        """
        query = (
            select(
                DocumentRow.id,
                DocumentRow.filename,
                DocumentRow.status,
                DocumentRow.created_at,
                UserRow.id.label("uploader_id"),
                UserRow.full_name.label("uploader_name"),
                UserRow.email.label("uploader_email"),
            )
            .join(UserRow, UserRow.id == DocumentRow.uploaded_by)
            .where(DocumentRow.org_id == self._org_id, UserRow.org_id == self._org_id)
            .order_by(DocumentRow.created_at.desc(), DocumentRow.id.desc())
            # One more than asked for: if it comes back, there is a next page.
            # Cheaper and more honest than a COUNT(*) over the whole table,
            # which is the other way to answer "is there more".
            .limit(limit + 1)
        )
        if after is not None:
            # Row-value comparison, not `created_at < x OR (created_at = x AND
            # id < y)`. Postgres drives an index scan straight from it, and it
            # is one expression rather than a disjunction to get subtly wrong.
            query = query.where(
                tuple_(DocumentRow.created_at, DocumentRow.id)
                < (after.created_at, after.id)
            )

        rows = (await self._session.execute(query)).all()
        page = rows[:limit]
        last = page[-1] if len(rows) > limit else None
        return DocumentPage(
            items=[_to_summary(row) for row in page],
            next_cursor=(
                DocumentCursor(created_at=last.created_at, id=last.id)
                if last is not None
                else None
            ),
        )

    # --- pipeline ---------------------------------------------------------
    # Written by the API on upload, and by pipeline workers thereafter. Workers
    # reach them through the same RLS-scoped session, pinned to the org their
    # workflow was started for.

    async def get(self, document_id: UUID) -> Document | None:
        """One document with its steps attached."""
        row = await self._session.scalar(
            select(DocumentRow).where(
                DocumentRow.id == document_id, DocumentRow.org_id == self._org_id
            )
        )
        if row is None:
            return None
        steps = (
            await self._session.scalars(
                select(DocumentStepRow).where(
                    DocumentStepRow.document_id == document_id,
                    DocumentStepRow.org_id == self._org_id,
                )
            )
        ).all()
        return _to_document(row, steps)

    async def _update(self, document_id: UUID, **values) -> None:
        await self._session.execute(
            update(DocumentRow)
            .where(DocumentRow.id == document_id, DocumentRow.org_id == self._org_id)
            .values(**values)
        )

    async def _update_step(self, document_id: UUID, step: Step, **values) -> None:
        await self._session.execute(
            update(DocumentStepRow)
            .where(
                DocumentStepRow.document_id == document_id,
                DocumentStepRow.step == step,
                DocumentStepRow.org_id == self._org_id,
            )
            .values(**values)
        )

    async def set_workflow_id(self, document_id: UUID, workflow_id: str) -> None:
        await self._update(document_id, workflow_id=workflow_id)

    async def start_step(self, document_id: UUID, step: Step, attempt: int) -> None:
        await self._update_step(
            document_id,
            step,
            status=StepStatus.RUNNING,
            attempts=attempt,
            started_at=func.coalesce(DocumentStepRow.started_at, func.now()),
        )
        # True exactly once in a document's life, so a conditional UPDATE beats
        # reading the row on every step of every attempt to find out.
        await self._session.execute(
            update(DocumentRow)
            .where(
                DocumentRow.id == document_id,
                DocumentRow.org_id == self._org_id,
                DocumentRow.status == DocumentStatus.UPLOADED,
            )
            .values(status=DocumentStatus.PROCESSING)
        )

    async def finish_step(
        self, document_id: UUID, step: Step, output: dict | None
    ) -> None:
        await self._update_step(
            document_id,
            step,
            status=StepStatus.SUCCEEDED,
            last_error=None,
            output=output,
            ended_at=func.now(),
        )

    async def record_step_error(
        self, document_id: UUID, step: Step, error: str
    ) -> None:
        await self._update_step(document_id, step, last_error=error)

    async def await_partner(self, document_id: UUID, partner_job_id: str) -> None:
        await self._update(
            document_id,
            partner_job_id=partner_job_id,
            status=DocumentStatus.AWAITING_PARTNER,
        )

    async def fail(self, document_id: UUID, step: Step) -> None:
        await self._update(document_id, status=DocumentStatus.FAILED, failed_step=step)
        await self._update_step(
            document_id, step, status=StepStatus.FAILED, ended_at=func.now()
        )


class UnscopedDocumentRepository:
    """Resolve a document by partner job id, before any org is known.

    The partner names no tenant - job_id is all it has - so this lookup cannot
    be RLS-scoped and runs on the system session, exactly like finding a user
    by email during login. The org is then *derived* from the row that comes
    back, never taken from the payload.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_partner_job_id(self, partner_job_id: str) -> Document | None:
        row = await self._session.scalar(
            select(DocumentRow).where(DocumentRow.partner_job_id == partner_job_id)
        )
        return _to_document(row) if row else None

    async def record_outcome(
        self,
        document_id: UUID,
        status: DocumentStatus,
        result: dict | None,
        occurred_at: datetime,
    ) -> None:
        """Apply the partner's answer: the status it implies, and its payload.

        One statement for both outcomes, because they differ only in the status
        written. The payload is stored either way - a failure's `result` is the
        only account of why the partner refused the document.
        """
        # The awaiting_partner predicate is the idempotency guarantee, and it
        # has to live here rather than in a status check the caller made
        # earlier: read-then-write is two statements, and two concurrent
        # deliveries of the same job_id both pass a check made in Python.
        #
        # As one conditional UPDATE it is a compare-and-swap. The second writer
        # blocks on the row lock, then re-evaluates this WHERE against the row
        # the first one committed - awaiting_partner no longer matches, so it
        # updates nothing. That re-evaluation is what makes READ COMMITTED
        # enough; nothing here needs a stricter isolation level or FOR UPDATE.
        await self._session.execute(
            update(DocumentRow)
            .where(
                DocumentRow.id == document_id,
                DocumentRow.status == DocumentStatus.AWAITING_PARTNER,
            )
            .values(
                status=status,
                partner_result=result,
                partner_occurred_at=occurred_at,
                failed_step=(
                    Step.EXTERNAL_CALL if status is DocumentStatus.FAILED else None
                ),
            )
        )
