"""Composition root.

Auth is a dependency, not ASGI middleware, for three reasons: middleware would
run on /health and /docs and need a path allowlist; dependencies show up in the
OpenAPI schema so Swagger gets a working Authorize button; and raising
HTTPException from a dependency beats hand-building a Response.
"""

import logging
from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.deps import AuthDeps, WebhookDeps
from app.application.upload_document import UploadDeps
from app.config import Settings, get_settings
from app.domain.auth import AuthContext
from app.domain.errors import InvalidAccessToken
from app.domain.ports import ObjectStore
from app.infrastructure.clock import SystemClock
from app.infrastructure.content_type import PureMagicDetector
from app.infrastructure.db.repositories import (
    OrgScopedDocumentRepository,
    OrgScopedOrganizationRepository,
    OrgScopedUserRepository,
    SqlAlchemyUnitOfWork,
    UnscopedRefreshTokenRepository,
    UnscopedUserRepository,
)
from app.infrastructure.db.session import Database, TenantUnitOfWork
from app.infrastructure.partner_jobs import DbPartnerJobSink
from app.infrastructure.progress import ProgressHub
from app.infrastructure.security.hashing import Argon2PasswordHasher
from app.infrastructure.security.jwt import PyJwtTokenService
from app.infrastructure.security.signatures import HmacSha256Signer
from app.observability import bind_context
from app.pipeline.runtime import TenantDocumentTransaction
from app.pipeline.workflow import DbosPipelineRunner

logger = logging.getLogger(__name__)

SettingsDep = Annotated[Settings, Depends(get_settings)]


def get_database(request: Request) -> Database:
    return request.app.state.database


DatabaseDep = Annotated[Database, Depends(get_database)]


@lru_cache
def _password_hasher() -> Argon2PasswordHasher:
    # Cached: the constructor computes a decoy hash, which is deliberately slow.
    return Argon2PasswordHasher()


def get_token_service(settings: SettingsDep) -> PyJwtTokenService:
    # Cheap to build (four fields), so no caching; that keeps it overridable in
    # tests without fighting a memoized instance.
    return PyJwtTokenService(
        secret=settings.jwt.secret.get_secret_value(),
        algorithm=settings.jwt.algorithm,
        issuer=settings.jwt.issuer,
        access_ttl=settings.jwt.access_ttl,
    )


TokenServiceDep = Annotated[PyJwtTokenService, Depends(get_token_service)]

_bearer = HTTPBearer(
    scheme_name="Bearer",
    description="Paste the access_token returned by POST /auth/login.",
    auto_error=False,
)


async def get_auth_context(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    tokens: TokenServiceDep,
) -> AuthContext:
    """Resolve user_id and org_id for the current request, or 401.

    `async` deliberately, even though nothing here awaits: FastAPI runs a *sync*
    dependency in a worker thread, which gets a copy of the context, so the
    contextvars bound below would be discarded on return and every log line
    would lose its tenant. Decoding an HS256 token is microseconds of CPU, so
    there is nothing to gain from the threadpool anyway.
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        ctx = tokens.decode_access_token(credentials.credentials)
    except InvalidAccessToken as exc:
        # Converted to HTTPException here, so it never reaches the domain-error
        # handler that logs the rest of the refusals. The reason matters: an
        # expired token is routine, a bad signature is not.
        logger.warning("auth.token.rejected", extra={"reason": str(exc)})
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    # Every log line for the rest of this request is now attributable to a
    # tenant and a user, without a single call site passing them along. This is
    # the one place both are known and trusted, so it is the only place that
    # should be binding them.
    bind_context(org_id=str(ctx.org_id), user_id=str(ctx.user_id))
    return ctx


CurrentUser = Annotated[AuthContext, Depends(get_auth_context)]


async def get_auth_deps(
    db: DatabaseDep,
    tokens: TokenServiceDep,
    settings: SettingsDep,
) -> AsyncIterator[AuthDeps]:
    """Wiring for the unauthenticated auth endpoints, on the system session."""
    async with db.system_session() as session:
        yield AuthDeps(
            users=UnscopedUserRepository(session),
            refresh_tokens=UnscopedRefreshTokenRepository(session),
            hasher=_password_hasher(),
            tokens=tokens,
            clock=SystemClock(),
            uow=SqlAlchemyUnitOfWork(session),
            refresh_ttl=settings.jwt.refresh_ttl,
        )


AuthDepsDep = Annotated[AuthDeps, Depends(get_auth_deps)]


async def get_tenant_session(
    ctx: CurrentUser, db: DatabaseDep
) -> AsyncIterator[AsyncSession]:
    """An RLS-scoped session pinned to the caller's organization."""
    async with db.tenant_session(ctx.org_id) as session:
        yield session


TenantSessionDep = Annotated[AsyncSession, Depends(get_tenant_session)]


def get_user_repository(
    session: TenantSessionDep, ctx: CurrentUser
) -> OrgScopedUserRepository:
    return OrgScopedUserRepository(session, ctx.org_id)


def get_organization_repository(
    session: TenantSessionDep, ctx: CurrentUser
) -> OrgScopedOrganizationRepository:
    return OrgScopedOrganizationRepository(session, ctx.org_id)


UserRepositoryDep = Annotated[OrgScopedUserRepository, Depends(get_user_repository)]
OrganizationRepositoryDep = Annotated[
    OrgScopedOrganizationRepository, Depends(get_organization_repository)
]

SIGNATURE_HEADER = "X-Partner-Signature"


def get_webhook_signer(settings: SettingsDep) -> HmacSha256Signer:
    return HmacSha256Signer(settings.partner.hmac_secret.get_secret_value())


WebhookSignerDep = Annotated[HmacSha256Signer, Depends(get_webhook_signer)]


async def verify_partner_signature(request: Request, signer: WebhookSignerDep) -> None:
    """Gate the webhook on HMAC over the raw body, before anything parses it.

    A dependency rather than the first lines of the handler: FastAPI solves
    dependencies before it validates the body, so an unsigned request is a 401
    and the parser never sees the payload. Reading the body here is free —
    Starlette caches it, so the body parameter still resolves.

    No WWW-Authenticate on the way out: this route is not part of the bearer
    surface and the partner has no token to offer.
    """
    signature = request.headers.get(SIGNATURE_HEADER)
    body = await request.body()
    if signature is None or not signer.verify(body, signature):
        # This endpoint is reachable without a token, so a failure here is the
        # one worth watching: it is either the partner misconfigured or someone
        # guessing at the secret. The signature itself is not logged -- it is a
        # (failed) authenticator, and the distinction below is the useful part.
        logger.warning(
            "webhook.partner.unverified",
            extra={"reason": "missing" if signature is None else "mismatch"},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"missing or invalid {SIGNATURE_HEADER}",
        )


def get_webhook_deps(settings: SettingsDep, db: DatabaseDep) -> WebhookDeps:
    # The real sink now that the pipeline issues job ids. It resolves
    # partner_job_id on the system session, because the partner names no
    # tenant. Nothing else about the webhook route changed.
    return WebhookDeps(
        sink=DbPartnerJobSink(db),
        clock=SystemClock(),
        tolerance=settings.partner.tolerance,
    )


WebhookDepsDep = Annotated[WebhookDeps, Depends(get_webhook_deps)]


def get_object_store(request: Request) -> ObjectStore:
    return request.app.state.object_store


ObjectStoreDep = Annotated[ObjectStore, Depends(get_object_store)]


def get_document_repository(
    session: TenantSessionDep, ctx: CurrentUser
) -> OrgScopedDocumentRepository:
    return OrgScopedDocumentRepository(session, ctx.org_id)


DocumentRepositoryDep = Annotated[
    OrgScopedDocumentRepository, Depends(get_document_repository)
]


async def get_upload_session(
    ctx: CurrentUser, db: DatabaseDep
) -> AsyncIterator[AsyncSession]:
    """An RLS-scoped session the handler commits itself.

    Not TenantSessionDep: that commits only when its block ends, which is after
    the response. The upload has to commit mid-handler so the pipeline it
    enqueues can see the rows it is about to write to.
    """
    async with db.tenant_session_manual(ctx.org_id) as session:
        yield session


UploadSessionDep = Annotated[AsyncSession, Depends(get_upload_session)]


def get_upload_deps(
    session: UploadSessionDep,
    ctx: CurrentUser,
    store: ObjectStoreDep,
    settings: SettingsDep,
) -> UploadDeps:
    return UploadDeps(
        documents=OrgScopedDocumentRepository(session, ctx.org_id),
        store=store,
        detector=PureMagicDetector(),
        clock=SystemClock(),
        max_bytes=settings.storage.max_upload_bytes,
        uow=TenantUnitOfWork(session, ctx.org_id),
        pipeline=DbosPipelineRunner(),
    )


UploadDepsDep = Annotated[UploadDeps, Depends(get_upload_deps)]


def get_document_transaction(
    ctx: CurrentUser, db: DatabaseDep
) -> TenantDocumentTransaction:
    """One short RLS-scoped transaction per call, for the caller's org."""
    return TenantDocumentTransaction(db, ctx.org_id)


DocumentTransactionDep = Annotated[
    TenantDocumentTransaction, Depends(get_document_transaction)
]


def get_progress_hub(request: Request) -> ProgressHub:
    return request.app.state.progress


ProgressHubDep = Annotated[ProgressHub, Depends(get_progress_hub)]
