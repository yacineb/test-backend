from fastapi import APIRouter, status

from app.api.deps import AuthDepsDep
from app.api.schemas import LoginRequest, RefreshRequest, TokenResponse
from app.application.login import login as login_use_case
from app.application.logout import logout as logout_use_case
from app.application.refresh import refresh as refresh_use_case
from app.domain.auth import TokenPair

router = APIRouter(prefix="/auth", tags=["auth"])


def _to_response(pair: TokenPair) -> TokenResponse:
    return TokenResponse(
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        expires_in=pair.expires_in,
    )


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Exchange email and password for a token pair",
)
async def login(body: LoginRequest, deps: AuthDepsDep) -> TokenResponse:
    pair = await login_use_case(deps, str(body.email), body.password)
    return _to_response(pair)


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Rotate a refresh token into a new pair",
    description=(
        "The presented refresh token is consumed. Replaying a consumed token "
        "revokes every token in its family."
    ),
)
async def refresh(body: RefreshRequest, deps: AuthDepsDep) -> TokenResponse:
    pair = await refresh_use_case(deps, body.refresh_token)
    return _to_response(pair)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke a refresh token family",
)
async def logout(body: RefreshRequest, deps: AuthDepsDep) -> None:
    await logout_use_case(deps, body.refresh_token)
