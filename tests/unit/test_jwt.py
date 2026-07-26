from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
import pytest

from app.domain.auth import AuthContext
from app.domain.errors import InvalidAccessToken
from tests.fakes import make_token_service

# Anchored to real now: PyJWT validates exp against the wall clock.
NOW = datetime.now(UTC)


def test_round_trip_preserves_user_and_org():
    service = make_token_service()
    ctx = AuthContext(user_id=uuid4(), org_id=uuid4())

    token, expires_in = service.issue_access_token(ctx, NOW)

    assert expires_in == 6 * 3600
    assert service.decode_access_token(token) == ctx


def test_expired_token_is_rejected():
    service = make_token_service()
    ctx = AuthContext(user_id=uuid4(), org_id=uuid4())
    token, _ = service.issue_access_token(ctx, NOW - timedelta(hours=7))

    with pytest.raises(InvalidAccessToken):
        service.decode_access_token(token)


def test_token_signed_with_another_secret_is_rejected():
    service = make_token_service()
    forged = jwt.encode(
        {
            "sub": str(uuid4()),
            "org": str(uuid4()),
            "typ": "access",
            "iss": "test-backend",
            "iat": int(NOW.timestamp()),
            "exp": int((NOW + timedelta(hours=1)).timestamp()),
        },
        "not-the-secret",
        algorithm="HS256",
    )

    with pytest.raises(InvalidAccessToken):
        service.decode_access_token(forged)


def test_unsigned_token_is_rejected():
    """alg=none must never be accepted."""
    service = make_token_service()
    forged = jwt.encode(
        {"sub": str(uuid4()), "org": str(uuid4()), "typ": "access"},
        key="",
        algorithm="none",
    )

    with pytest.raises(InvalidAccessToken):
        service.decode_access_token(forged)


def test_non_access_token_type_is_rejected():
    service = make_token_service()
    forged = jwt.encode(
        {
            "sub": str(uuid4()),
            "org": str(uuid4()),
            "typ": "refresh",
            "iss": "test-backend",
            "iat": int(NOW.timestamp()),
            "exp": int((NOW + timedelta(hours=1)).timestamp()),
        },
        "test-secret",
        algorithm="HS256",
    )

    with pytest.raises(InvalidAccessToken):
        service.decode_access_token(forged)


def test_refresh_token_hash_is_stable_and_secret_is_not_stored():
    service = make_token_service()

    secret, token_hash = service.generate_refresh_token()

    assert secret != token_hash
    assert len(token_hash) == 64
    assert service.hash_refresh_token(secret) == token_hash


def test_refresh_tokens_are_unique():
    service = make_token_service()

    secrets = {service.generate_refresh_token()[0] for _ in range(100)}

    assert len(secrets) == 100
