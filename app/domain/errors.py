"""Domain error hierarchy.

The API layer maps these to status codes in app/api/errors.py. Nothing in the
domain knows what an HTTP status code is.
"""


class DomainError(Exception):
    """Base class for every error the domain raises."""


class AuthenticationError(DomainError):
    """Credentials or tokens were rejected."""


class InvalidCredentials(AuthenticationError):
    """Unknown email or wrong password. Deliberately does not say which."""


class InactiveUser(AuthenticationError):
    """The user exists and authenticated, but the account is disabled."""


class InvalidRefreshToken(AuthenticationError):
    """Refresh token is unknown, expired, or revoked."""


class RefreshTokenReused(AuthenticationError):
    """An already-consumed refresh token was replayed.

    Treated as theft: the whole token family is revoked before raising.
    """


class InvalidAccessToken(AuthenticationError):
    """Bearer token failed signature, expiry, or claim validation."""
