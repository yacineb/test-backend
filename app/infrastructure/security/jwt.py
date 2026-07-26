import hashlib
import secrets
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import jwt

from app.domain.auth import AuthContext
from app.domain.errors import InvalidAccessToken

_ACCESS_TYP = "access"


class PyJwtTokenService:
    """Access tokens are signed JWTs; refresh tokens are opaque random strings.

    Refresh tokens are deliberately *not* JWTs: they must be revocable, which
    means a database lookup either way, and an opaque value leaks nothing if it
    ends up in a log.
    """

    def __init__(
        self,
        secret: str,
        algorithm: str,
        issuer: str,
        access_ttl: timedelta,
    ) -> None:
        self._secret = secret
        self._algorithm = algorithm
        self._issuer = issuer
        self._access_ttl = access_ttl

    def issue_access_token(self, ctx: AuthContext, now: datetime) -> tuple[str, int]:
        expires_at = now + self._access_ttl
        payload = {
            "sub": str(ctx.user_id),
            "org": str(ctx.org_id),
            "typ": _ACCESS_TYP,
            "iss": self._issuer,
            "iat": int(now.timestamp()),
            "exp": int(expires_at.timestamp()),
            "jti": uuid4().hex,
        }
        token = jwt.encode(payload, self._secret, algorithm=self._algorithm)
        return token, int(self._access_ttl.total_seconds())

    def decode_access_token(self, token: str) -> AuthContext:
        try:
            payload = jwt.decode(
                token,
                self._secret,
                algorithms=[self._algorithm],
                issuer=self._issuer,
                options={"require": ["exp", "iat", "sub", "iss"]},
            )
        except jwt.PyJWTError as exc:
            raise InvalidAccessToken("access token is not valid") from exc

        # A refresh token must never be usable as a bearer credential. Nothing
        # else currently mints typ != access, but the check is one line and the
        # failure it prevents is total.
        if payload.get("typ") != _ACCESS_TYP:
            raise InvalidAccessToken("wrong token type")

        try:
            return AuthContext(
                user_id=UUID(payload["sub"]),
                org_id=UUID(payload["org"]),
            )
        except (KeyError, ValueError) as exc:
            raise InvalidAccessToken("malformed token claims") from exc

    def generate_refresh_token(self) -> tuple[str, str]:
        secret = secrets.token_urlsafe(48)
        return secret, self.hash_refresh_token(secret)

    def hash_refresh_token(self, token: str) -> str:
        # SHA-256, not argon2: this is a 384-bit random value, not a password.
        # There is nothing to brute-force, and refresh runs on every rotation.
        return hashlib.sha256(token.encode()).hexdigest()
