from app.application.deps import AuthDeps
from app.application.tokens import issue_token_pair
from app.domain.auth import TokenPair
from app.domain.errors import InactiveUser, InvalidRefreshToken, RefreshTokenReused


async def refresh(deps: AuthDeps, refresh_token: str) -> TokenPair:
    """Rotate a refresh token.

    The presented token is consumed and a new one from the same family is
    returned. Replaying a token that was already consumed means either the
    client or an attacker holds a copy, so the entire family is revoked.
    """
    now = deps.clock.now()
    stored = await deps.refresh_tokens.get_by_hash(
        deps.tokens.hash_refresh_token(refresh_token)
    )

    if stored is None or stored.revoked_at is not None:
        raise InvalidRefreshToken("refresh token is not valid")

    if stored.consumed_at is not None:
        await deps.refresh_tokens.revoke_family(stored.family_id, now)
        # Commit before raising: the exception unwinds the session, and an
        # uncommitted revocation would leave the stolen family usable.
        await deps.uow.commit()
        raise RefreshTokenReused("refresh token replayed; session revoked")

    if stored.is_expired(now):
        raise InvalidRefreshToken("refresh token has expired")

    user = await deps.users.get_by_id(stored.user_id)
    if user is None:
        raise InvalidRefreshToken("refresh token is not valid")
    if not user.is_active:
        raise InactiveUser("account is disabled")

    await deps.refresh_tokens.consume(stored.id, now)

    pair = await issue_token_pair(deps, user.id, user.org_id, stored.family_id)
    await deps.uow.commit()
    return pair
