from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class AuthContext:
    """Who is making the current request. Carried by every authenticated route."""

    user_id: UUID
    org_id: UUID


@dataclass(frozen=True, slots=True)
class TokenPair:
    access_token: str
    refresh_token: str
    expires_in: int


@dataclass(frozen=True, slots=True)
class RefreshToken:
    """A stored refresh token. Only the hash of the secret ever reaches the DB.

    `family_id` chains every rotation of one login together so that replaying a
    consumed token can revoke all of its descendants at once.
    """

    id: UUID
    user_id: UUID
    org_id: UUID
    family_id: UUID
    token_hash: str
    issued_at: datetime
    expires_at: datetime
    consumed_at: datetime | None = None
    revoked_at: datetime | None = None

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expires_at
