"""Composition root.

Auth is a dependency, not ASGI middleware, for three reasons: middleware would
run on /health and /docs and need a path allowlist; dependencies show up in the
OpenAPI schema so Swagger gets a working Authorize button; and raising
HTTPException from a dependency beats hand-building a Response.
"""

from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.deps import AuthDeps, WebhookDeps
from app.config import Settings, get_settings
from app.domain.auth import AuthContext
from app.domain.errors import InvalidAccessToken
from app.infrastructure.clock import SystemClock
from app.infrastructure.db.repositories import (
    OrgScopedOrganizationRepository,
    OrgScopedUserRepository,
    SqlAlchemyUnitOfWork,
    UnscopedRefreshTokenRepository,
    UnscopedUserRepository,
)
from app.infrastructure.db.session import Database
from app.infrastructure.partner_jobs import InMemoryPartnerJobSink
from app.infrastructure.security.hashing import Argon2PasswordHasher
from app.infrastructure.security.jwt import PyJwtTokenService
from app.infrastructure.security.signatures import HmacSha256Signer

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


def get_auth_context(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    tokens: TokenServiceDep,
) -> AuthContext:
    """Resolve user_id and org_id for the current request, or 401."""
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return tokens.decode_access_token(credentials.credentials)
    except InvalidAccessToken as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


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
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"missing or invalid {SIGNATURE_HEADER}",
        )


@lru_cache
def _partner_job_sink() -> InMemoryPartnerJobSink:
    # Cached so the stub's memory survives between requests. Replaced wholesale
    # when the pipeline lands; see app/infrastructure/partner_jobs.py.
    return InMemoryPartnerJobSink()


def get_webhook_deps(settings: SettingsDep) -> WebhookDeps:
    return WebhookDeps(
        sink=_partner_job_sink(),
        clock=SystemClock(),
        tolerance=settings.partner.tolerance,
    )


WebhookDepsDep = Annotated[WebhookDeps, Depends(get_webhook_deps)]
