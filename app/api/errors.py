"""Domain error -> HTTP status. The only place that mapping is allowed to live."""

import logging

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from app.domain.errors import (
    DocumentNotFound,
    DocumentNotReady,
    EmptyUpload,
    InactiveUser,
    InvalidAccessToken,
    InvalidCredentials,
    InvalidRefreshToken,
    MissingFilename,
    RefreshTokenReused,
    StaleWebhook,
    UnknownPartnerJob,
    UnsupportedFileType,
    UploadTooLarge,
)

logger = logging.getLogger(__name__)

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
    # 409, not 404: the document is there and the caller may see it, but its
    # extraction is not finished. A 404 would say the wrong thing to a client
    # polling for data that is on its way.
    DocumentNotReady: status.HTTP_409_CONFLICT,
    UploadTooLarge: status.HTTP_413_CONTENT_TOO_LARGE,
    EmptyUpload: status.HTTP_400_BAD_REQUEST,
    MissingFilename: status.HTTP_400_BAD_REQUEST,
    UnsupportedFileType: status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
}


def register_error_handlers(app: FastAPI) -> None:
    for error_type, status_code in _STATUS_BY_ERROR.items():
        app.add_exception_handler(error_type, _handler(status_code))


def _handler(status_code: int):
    async def handle(_: Request, exc: Exception) -> JSONResponse:
        # Every refusal is logged in one place rather than at each raise site.
        # The exception message already carries the specifics (which limit, what
        # the file sniffed as), so the call sites stay free of logging noise.
        #
        # WARNING, not ERROR: these are the API telling a client it got
        # something wrong, which is the system working. Paging on a rejected
        # PNG is how alerting gets ignored. They are still worth seeing in
        # aggregate -- a spike in 401s or 413s is a different story entirely.
        logger.warning(
            "request.rejected",
            extra={
                "status": status_code,
                "error": type(exc).__name__,
                "detail": str(exc),
            },
        )
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
