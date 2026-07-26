import pytest

from app.application.login import login
from app.application.logout import logout
from app.application.refresh import refresh
from app.domain.errors import InvalidRefreshToken
from tests.fakes import make_deps, make_user

pytestmark = pytest.mark.anyio


async def test_logout_revokes_the_family():
    deps, refresh_tokens, _ = make_deps([make_user()])
    pair = await login(deps, "alice@acme.example.com", "password123")

    await logout(deps, pair.refresh_token)

    assert all(t.revoked_at is not None for t in refresh_tokens.tokens.values())


async def test_logged_out_token_cannot_refresh():
    deps, _, _ = make_deps([make_user()])
    pair = await login(deps, "alice@acme.example.com", "password123")

    await logout(deps, pair.refresh_token)

    with pytest.raises(InvalidRefreshToken):
        await refresh(deps, pair.refresh_token)


async def test_logout_kills_every_rotation_of_the_session():
    deps, _, _ = make_deps([make_user()])
    first = await login(deps, "alice@acme.example.com", "password123")
    second = await refresh(deps, first.refresh_token)

    await logout(deps, second.refresh_token)

    with pytest.raises(InvalidRefreshToken):
        await refresh(deps, second.refresh_token)


async def test_logout_is_silent_about_unknown_tokens():
    deps, _, uow = make_deps([make_user()])

    await logout(deps, "not-a-real-token")

    assert uow.commits == 0
