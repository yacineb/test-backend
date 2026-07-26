"""In-memory adapters.

These exist because the use cases depend on Protocols rather than SQLAlchemy,
so the whole auth flow is testable without a database. Transaction semantics
are *not* modelled here — commit() is a counter. Anything that depends on real
rollback behaviour belongs in tests/integration.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from app.application.deps import AuthDeps
from app.domain.auth import RefreshToken
from app.domain.organization import Organization
from app.domain.user import User
from app.infrastructure.security.jwt import PyJwtTokenService


class FakeClock:
    """Controllable time, anchored to *real* now by default.

    Not a fixed date: issued JWTs are validated by PyJWT against the wall
    clock, so a hardcoded anchor would silently mint pre-expired tokens the
    moment that date slid into the past. Tests control relative offsets via
    advance(); only the offsets matter.
    """

    def __init__(self, now: datetime | None = None) -> None:
        self._now = now or datetime.now(UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


class FakeHasher:
    """Reversible on purpose: these tests are about flow, not about argon2."""

    def hash(self, password: str) -> str:
        return f"hashed:{password}"

    def verify(self, password: str, password_hash: str) -> bool:
        return password_hash == f"hashed:{password}"

    def verify_dummy(self, password: str) -> None:
        return None


class FakeUserRepository:
    def __init__(self, users: list[User] | None = None) -> None:
        self._users = {user.id: user for user in users or []}

    def add(self, user: User) -> None:
        self._users[user.id] = user

    async def get_by_email(self, email: str) -> User | None:
        return next((u for u in self._users.values() if u.email == email), None)

    async def get_by_id(self, user_id: UUID) -> User | None:
        return self._users.get(user_id)


class FakeOrganizationRepository:
    def __init__(self, organizations: list[Organization] | None = None) -> None:
        self._orgs = {org.id: org for org in organizations or []}

    async def get(self, org_id: UUID) -> Organization | None:
        return self._orgs.get(org_id)


class FakeRefreshTokenRepository:
    def __init__(self) -> None:
        self.tokens: dict[UUID, RefreshToken] = {}

    async def add(self, token: RefreshToken) -> None:
        self.tokens[token.id] = token

    async def get_by_hash(self, token_hash: str) -> RefreshToken | None:
        return next(
            (t for t in self.tokens.values() if t.token_hash == token_hash), None
        )

    async def consume(self, token_id: UUID, now: datetime) -> None:
        token = self.tokens[token_id]
        if token.consumed_at is None:
            self.tokens[token_id] = replace(token, consumed_at=now)

    async def revoke_family(self, family_id: UUID, now: datetime) -> None:
        for token_id, token in self.tokens.items():
            if token.family_id == family_id and token.revoked_at is None:
                self.tokens[token_id] = replace(token, revoked_at=now)


class FakeUnitOfWork:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


def make_token_service() -> PyJwtTokenService:
    return PyJwtTokenService(
        secret="test-secret-at-least-32-bytes-long!!",
        algorithm="HS256",
        issuer="test-backend",
        access_ttl=timedelta(hours=6),
    )


def make_user(
    *,
    org_id: UUID | None = None,
    email: str = "alice@acme.example.com",
    password: str = "password123",
    is_active: bool = True,
) -> User:
    return User(
        id=uuid4(),
        org_id=org_id or uuid4(),
        email=email,
        full_name="Alice Martin",
        password_hash=FakeHasher().hash(password),
        is_active=is_active,
    )


def make_deps(
    users: list[User],
    clock: FakeClock | None = None,
    refresh_ttl: timedelta = timedelta(days=30),
) -> tuple[AuthDeps, FakeRefreshTokenRepository, FakeUnitOfWork]:
    refresh_tokens = FakeRefreshTokenRepository()
    uow = FakeUnitOfWork()
    deps = AuthDeps(
        users=FakeUserRepository(users),
        refresh_tokens=refresh_tokens,
        hasher=FakeHasher(),
        tokens=make_token_service(),
        clock=clock or FakeClock(),
        uow=uow,
        refresh_ttl=refresh_ttl,
    )
    return deps, refresh_tokens, uow
