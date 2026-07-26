from app.application.deps import AuthDeps


async def logout(deps: AuthDeps, refresh_token: str) -> None:
    """Revoke the whole family the token belongs to.

    Idempotent and silent about unknown tokens: logging out is not an operation
    a caller should be able to probe for valid token values.
    """
    stored = await deps.refresh_tokens.get_by_hash(
        deps.tokens.hash_refresh_token(refresh_token)
    )
    if stored is None:
        return

    await deps.refresh_tokens.revoke_family(stored.family_id, deps.clock.now())
    await deps.uow.commit()
