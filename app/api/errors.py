"""Domain error -> HTTP status. The only place that mapping is allowed to live."""

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from app.domain.errors import (
    InactiveUser,
    InvalidAccessToken,
    InvalidCredentials,
    InvalidRefreshToken,
    RefreshTokenReused,
)

_STATUS_BY_ERROR = {
    InvalidCredentials: status.HTTP_401_UNAUTHORIZED,
    InvalidAccessToken: status.HTTP_401_UNAUTHORIZED,
    InvalidRefreshToken: status.HTTP_401_UNAUTHORIZED,
    RefreshTokenReused: status.HTTP_401_UNAUTHORIZED,
    InactiveUser: status.HTTP_403_FORBIDDEN,
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
