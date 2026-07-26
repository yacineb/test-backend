"""Domain error -> HTTP status. The only place that mapping is allowed to live."""

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from app.domain.errors import (
    DocumentNotFound,
    EmptyUpload,
    InactiveUser,
    InvalidAccessToken,
    InvalidCredentials,
    InvalidRefreshToken,
    MissingFilename,
    RefreshTokenReused,
    StaleWebhook,
    UnknownPartnerJob,
    UploadTooLarge,
)

# ObjectNotFound is deliberately absent: no route reads from the object store
# yet, so a mapping for it could never fire. It arrives with the download
# endpoint that needs it.
_STATUS_BY_ERROR = {
    InvalidCredentials: status.HTTP_401_UNAUTHORIZED,
    InvalidAccessToken: status.HTTP_401_UNAUTHORIZED,
    InvalidRefreshToken: status.HTTP_401_UNAUTHORIZED,
    RefreshTokenReused: status.HTTP_401_UNAUTHORIZED,
    InactiveUser: status.HTTP_403_FORBIDDEN,
    # Retrying the same bytes will not help in either case: the timestamp is
    # signed, and an unknown job_id is one we never issued.
    StaleWebhook: status.HTTP_400_BAD_REQUEST,
    UnknownPartnerJob: status.HTTP_404_NOT_FOUND,
    # Same reasoning as UnknownPartnerJob: a 403 here would confirm that
    # the document exists in some other tenant.
    DocumentNotFound: status.HTTP_404_NOT_FOUND,
    UploadTooLarge: status.HTTP_413_CONTENT_TOO_LARGE,
    EmptyUpload: status.HTTP_400_BAD_REQUEST,
    MissingFilename: status.HTTP_400_BAD_REQUEST,
}


def register_error_handlers(app: FastAPI) -> None:
    for error_type, status_code in _STATUS_BY_ERROR.items():
        app.add_exception_handler(error_type, _handler(status_code))


def _handler(status_code: int):
    async def handle(_: Request, exc: Exception) -> JSONResponse:
        headers = (
            {"WWW-Authenticate": "Bearer"}
            if status_code == status.HTTP_401_UNAUTHORIZED
            else None
        )
        return JSONResponse(
            status_code=status_code,
            content={"detail": str(exc)},
            headers=headers,
        )

    return handle
