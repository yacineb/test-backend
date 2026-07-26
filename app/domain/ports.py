"""Outbound ports.

Protocols, not ABCs: adapters satisfy them structurally, so infrastructure
never imports the domain to inherit from it, and tests can hand-roll fakes.
"""

from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.domain.auth import AuthContext, RefreshToken
from app.domain.organization import Organization
from app.domain.partner import PartnerNotification
from app.domain.user import User


class Clock(Protocol):
    def now(self) -> datetime: ...


class PasswordHasher(Protocol):
    def hash(self, password: str) -> str: ...

    def verify(self, password: str, password_hash: str) -> bool: ...

    def verify_dummy(self, password: str) -> None:
        """Burn the same time a real verify costs, for unknown-user paths."""
        ...


class TokenService(Protocol):
    def issue_access_token(self, ctx: AuthContext, now: datetime) -> tuple[str, int]:
        """Return the signed JWT and its lifetime in seconds."""
        ...

    def decode_access_token(self, token: str) -> AuthContext:
        """Raise InvalidAccessToken if the token is not usable right now."""
        ...

    def generate_refresh_token(self) -> tuple[str, str]:
        """Return (secret to hand the client, hash to store)."""
        ...

    def hash_refresh_token(self, token: str) -> str: ...


class UnitOfWork(Protocol):
    async def commit(self) -> None:
        """Make pending writes durable.

        Explicit because one write must outlive the exception raised right
        after it: revoking a token family on replay detection.
        """
        ...


class UserRepository(Protocol):
    async def get_by_email(self, email: str) -> User | None: ...

    async def get_by_id(self, user_id: UUID) -> User | None: ...


class OrganizationRepository(Protocol):
    async def get(self, org_id: UUID) -> Organization | None: ...


class RefreshTokenRepository(Protocol):
    async def add(self, token: RefreshToken) -> None: ...

    async def get_by_hash(self, token_hash: str) -> RefreshToken | None: ...

    async def consume(self, token_id: UUID, now: datetime) -> None: ...

    async def revoke_family(self, family_id: UUID, now: datetime) -> None: ...


class PartnerJobSink(Protocol):
    async def deliver(self, notification: PartnerNotification) -> None:
        """Apply a verified partner outcome to the document holding job_id.

        Raise UnknownPartnerJob when nothing is waiting on that job_id. Must be
        idempotent: partners retry, so the same job_id will arrive twice and a
        duplicate must not apply the outcome twice.
        """
        ...
