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


class WebhookError(DomainError):
    """An inbound partner notification was rejected."""


class StaleWebhook(WebhookError):
    """occurred_at sits outside the accepted replay window."""


class UnknownPartnerJob(WebhookError):
    """No document is waiting on that job_id."""


class UploadError(DomainError):
    """An upload was rejected before it became a document."""


class UploadTooLarge(UploadError):
    """The file exceeds the configured per-upload limit."""

    def __init__(self, limit: int) -> None:
        super().__init__(f"file exceeds the {limit} byte limit")


class EmptyUpload(UploadError):
    """A file part was present but carried no bytes."""

    def __init__(self) -> None:
        super().__init__("file is empty")


class MissingFilename(UploadError):
    """A file part arrived without a name to record it under."""

    def __init__(self) -> None:
        super().__init__("file must have a filename")


class UnsupportedFileType(UploadError):
    """The bytes are not a PDF, whatever the client's Content-Type claimed."""

    def __init__(self, detected: str | None) -> None:
        super().__init__(
            f"only application/pdf is accepted; the file's contents look like "
            f"{detected or 'an unrecognised format'}"
        )


class ObjectNotFound(DomainError):
    """Read of a storage key that does not exist."""


class DocumentNotFound(DomainError):
    """No document with that id in the caller's organization.

    Deliberately not distinguished from "exists but belongs to someone else":
    telling those apart is a cross-tenant existence oracle.
    """


class DocumentNotReady(DomainError):
    """The document exists, but its extraction has not finished.

    Carries the current status, so the caller learns what it is waiting for
    instead of being told only that it cannot have the data.
    """

    def __init__(self, status: str) -> None:
        super().__init__(f"no extracted data yet: the document is {status}")
