from dataclasses import replace
from datetime import timedelta

import pytest

from app.application.login import login
from app.application.refresh import refresh
from app.domain.errors import InactiveUser, InvalidRefreshToken, RefreshTokenReused
from tests.fakes import FakeClock, make_deps, make_user

pytestmark = pytest.mark.anyio


async def test_refresh_rotates_the_token():
    deps, refresh_tokens, _ = make_deps([make_user()])
    first = await login(deps, "alice@acme.example.com", "password123")

    second = await refresh(deps, first.refresh_token)

    assert second.refresh_token != first.refresh_token
    assert len(refresh_tokens.tokens) == 2


async def test_rotation_stays_in_the_same_family():
    deps, refresh_tokens, _ = make_deps([make_user()])
    first = await login(deps, "alice@acme.example.com", "password123")

    await refresh(deps, first.refresh_token)

    families = {t.family_id for t in refresh_tokens.tokens.values()}
    assert len(families) == 1


async def test_used_token_is_marked_consumed():
    deps, refresh_tokens, _ = make_deps([make_user()])
    first = await login(deps, "alice@acme.example.com", "password123")
    used_hash = deps.tokens.hash_refresh_token(first.refresh_token)

    await refresh(deps, first.refresh_token)

    used = next(t for t in refresh_tokens.tokens.values() if t.token_hash == used_hash)
    assert used.consumed_at is not None


async def test_unknown_token_is_rejected():
    deps, _, _ = make_deps([make_user()])

    with pytest.raises(InvalidRefreshToken):
        await refresh(deps, "not-a-real-token")


async def test_expired_token_is_rejected():
    clock = FakeClock()
    deps, _, _ = make_deps([make_user()], clock=clock, refresh_ttl=timedelta(days=1))
    first = await login(deps, "alice@acme.example.com", "password123")

    clock.advance(timedelta(days=1, seconds=1))

    with pytest.raises(InvalidRefreshToken):
        await refresh(deps, first.refresh_token)


async def test_replay_raises_and_revokes_the_whole_family():
    deps, refresh_tokens, _ = make_deps([make_user()])
    first = await login(deps, "alice@acme.example.com", "password123")
    second = await refresh(deps, first.refresh_token)

    # The attacker replays the token the legitimate client already spent.
    with pytest.raises(RefreshTokenReused):
        await refresh(deps, first.refresh_token)

    assert all(t.revoked_at is not None for t in refresh_tokens.tokens.values())
    # ...which also locks out the legitimate client's current token.
    with pytest.raises(InvalidRefreshToken):
        await refresh(deps, second.refresh_token)


async def test_replay_commits_the_revocation_before_raising():
    """The revocation must be durable; it is followed immediately by a raise."""
    deps, _, uow = make_deps([make_user()])
    first = await login(deps, "alice@acme.example.com", "password123")
    await refresh(deps, first.refresh_token)
    commits_before = uow.commits

    with pytest.raises(RefreshTokenReused):
        await refresh(deps, first.refresh_token)

    assert uow.commits == commits_before + 1


async def test_deactivated_user_cannot_refresh():
    user = make_user()
    deps, _, _ = make_deps([user])
    first = await login(deps, "alice@acme.example.com", "password123")

    deps.users.add(replace(user, is_active=False))

    with pytest.raises(InactiveUser):
        await refresh(deps, first.refresh_token)
