"""HmacSha256Signer, checked against hmac itself rather than against itself."""

import hmac
from hashlib import sha256

from app.infrastructure.security.signatures import HmacSha256Signer

SECRET = "partner-secret-at-least-32-bytes-ok!"
BODY = b'{"job_id":"j_abc123def4567890","status":"completed"}'


def test_signature_matches_an_independently_computed_hmac():
    expected = hmac.new(SECRET.encode(), BODY, sha256).hexdigest()

    assert HmacSha256Signer(SECRET).sign(BODY) == expected
    assert len(expected) == 64


def test_verify_accepts_its_own_signature():
    signer = HmacSha256Signer(SECRET)

    assert signer.verify(BODY, signer.sign(BODY))


def test_verify_rejects_a_body_changed_by_one_byte():
    signer = HmacSha256Signer(SECRET)
    signature = signer.sign(BODY)

    assert not signer.verify(BODY + b" ", signature)


def test_verify_rejects_a_signature_from_another_secret():
    signature = HmacSha256Signer("some-other-secret").sign(BODY)

    assert not HmacSha256Signer(SECRET).verify(BODY, signature)


def test_verify_tolerates_case_and_surrounding_whitespace():
    """Hex is hex. Being strict about it only breaks well-meaning partners."""
    signer = HmacSha256Signer(SECRET)
    signature = signer.sign(BODY)

    assert signer.verify(BODY, f"  {signature.upper()}  ")


def test_verify_rejects_junk_without_raising():
    signer = HmacSha256Signer(SECRET)

    # compare_digest raises TypeError on non-ASCII str; that must not become a
    # 500 just because someone sent an emoji.
    assert not signer.verify(BODY, "")
    assert not signer.verify(BODY, "not-hex")
    assert not signer.verify(BODY, "signature-ééé")
