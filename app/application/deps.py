from dataclasses import dataclass
from datetime import timedelta

from app.domain.ports import (
    Clock,
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
