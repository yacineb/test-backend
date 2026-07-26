from dataclasses import dataclass
from datetime import timedelta

from app.domain.ports import (
    Clock,
    PartnerJobSink,
    PasswordHasher,
    RefreshTokenRepository,
    TokenService,
    UnitOfWork,
    UserRepository,
)


@dataclass(frozen=True, slots=True)
class AuthDeps:
    """Everything the auth use cases need, bundled so signatures stay short."""

    users: UserRepository
    refresh_tokens: RefreshTokenRepository
    hasher: PasswordHasher
    tokens: TokenService
    clock: Clock
    uow: UnitOfWork
    refresh_ttl: timedelta


@dataclass(frozen=True, slots=True)
class WebhookDeps:
    """What the inbound webhook use case needs. No repositories: the sink owns
    persistence, and the signature was already checked at the edge."""

    sink: PartnerJobSink
    clock: Clock
    tolerance: timedelta
