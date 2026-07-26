"""Outbound ports.

Protocols, not ABCs: adapters satisfy them structurally, so infrastructure
never imports the domain to inherit from it, and tests can hand-roll fakes.
"""

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.domain.auth import AuthContext, RefreshToken
from app.domain.document import Document
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


class DocumentRepository(Protocol):
    async def add(self, document: Document) -> None:
        """Persist a document and flush, so violations surface to the caller."""
        ...

    async def list_recent(self, limit: int, offset: int) -> list[Document]:
        """Newest first, already scoped to one organization by the adapter."""
        ...


class ObjectStore(Protocol):
    """Blob storage for document content.

    The contract every backend must honour:

        A key either exists complete, or does not exist. There is no partial
        object, under any failure.

    That is why `put` owns the whole write/commit/abort lifecycle instead of
    handing back a writer: on POSIX the commit is an atomic rename, on S3 it is
    CompleteMultipartUpload, and neither can be driven correctly by a caller
    holding a file handle.
    """

    async def put(self, key: str, chunks: AsyncIterator[bytes]) -> int:
        """Consume `chunks` and commit them at `key`. Returns bytes written.

        If `chunks` raises, nothing is committed and no debris is left behind.
        """
        ...

    def get(self, key: str) -> AsyncIterator[bytes]:
        """Stream the object at `key`, or raise `ObjectNotFound`."""
        ...

    async def delete(self, key: str) -> None:
        """Remove `key`. Idempotent: deleting a missing key is not an error."""
        ...
