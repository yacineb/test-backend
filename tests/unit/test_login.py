import pytest

from app.application.login import login
from app.domain.errors import InactiveUser, InvalidCredentials
from tests.fakes import make_deps, make_user

pytestmark = pytest.mark.anyio


async def test_login_returns_a_token_pair():
    user = make_user()
    deps, refresh_tokens, uow = make_deps([user])

    pair = await login(deps, "alice@acme.example.com", "password123")

    assert pair.access_token
    assert pair.refresh_token
    assert pair.expires_in == 6 * 3600
    assert len(refresh_tokens.tokens) == 1
    assert uow.commits == 1


async def test_access_token_carries_user_and_org():
    user = make_user()
    deps, _, _ = make_deps([user])

    pair = await login(deps, "alice@acme.example.com", "password123")
    ctx = deps.tokens.decode_access_token(pair.access_token)

    assert ctx.user_id == user.id
    assert ctx.org_id == user.org_id


async def test_email_is_normalised_before_lookup():
    deps, _, _ = make_deps([make_user(email="alice@acme.example.com")])

    pair = await login(deps, "  Alice@Acme.Example.COM  ", "password123")

    assert pair.access_token


async def test_wrong_password_is_rejected():
    deps, _, uow = make_deps([make_user()])

    with pytest.raises(InvalidCredentials):
        await login(deps, "alice@acme.example.com", "wrong")

    assert uow.commits == 0


async def test_unknown_email_is_rejected():
    deps, _, _ = make_deps([])

    with pytest.raises(InvalidCredentials):
        await login(deps, "nobody@acme.example.com", "password123")


async def test_unknown_email_and_wrong_password_are_indistinguishable():
    deps, _, _ = make_deps([make_user()])

    with pytest.raises(InvalidCredentials) as unknown:
        await login(deps, "nobody@acme.example.com", "password123")
    with pytest.raises(InvalidCredentials) as wrong:
        await login(deps, "alice@acme.example.com", "wrong")

    assert str(unknown.value) == str(wrong.value)


async def test_disabled_account_is_rejected_after_password_check():
    deps, _, _ = make_deps([make_user(is_active=False)])

    with pytest.raises(InactiveUser):
        await login(deps, "alice@acme.example.com", "password123")


async def test_each_login_starts_a_new_token_family():
    deps, refresh_tokens, _ = make_deps([make_user()])

    await login(deps, "alice@acme.example.com", "password123")
    await login(deps, "alice@acme.example.com", "password123")

    families = {t.family_id for t in refresh_tokens.tokens.values()}
    assert len(families) == 2
