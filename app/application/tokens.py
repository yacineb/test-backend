from uuid import UUID, uuid4

from app.application.deps import AuthDeps
from app.domain.auth import AuthContext, RefreshToken, TokenPair


async def issue_token_pair(
    deps: AuthDeps,
    user_id: UUID,
    org_id: UUID,
    family_id: UUID | None = None,
) -> TokenPair:
    """Mint an access JWT plus a fresh refresh token.

    Passing `family_id` continues an existing rotation chain; omitting it starts
    a new one, which is what a password login does.
    """
    now = deps.clock.now()
    ctx = AuthContext(user_id=user_id, org_id=org_id)
    access_token, expires_in = deps.tokens.issue_access_token(ctx, now)
    secret, token_hash = deps.tokens.generate_refresh_token()

    await deps.refresh_tokens.add(
        RefreshToken(
            id=uuid4(),
            user_id=user_id,
            org_id=org_id,
            family_id=family_id or uuid4(),
            token_hash=token_hash,
            issued_at=now,
            expires_at=now + deps.refresh_ttl,
        )
    )

    return TokenPair(
        access_token=access_token,
        refresh_token=secret,
        expires_in=expires_in,
    )
