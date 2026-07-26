"""Hex HMAC-SHA256 over a request body."""

import hmac
from hashlib import sha256


class HmacSha256Signer:
    """Signs and verifies exactly the bytes on the wire.

    Takes bytes, never a model: the partner signed its own encoding, and
    re-serializing a parsed body changes whitespace and key order, so a
    signature computed over json.dumps(parsed) would reject valid requests.
    """

    def __init__(self, secret: str) -> None:
        self._secret = secret.encode()

    def sign(self, body: bytes) -> str:
        return hmac.new(self._secret, body, sha256).hexdigest()

    def verify(self, body: bytes, signature: str) -> bool:
        candidate = signature.strip().lower()
        # compare_digest raises on non-ASCII str; a hostile header is a False,
        # not a 500.
        if not candidate.isascii():
            return False
        return hmac.compare_digest(self.sign(body), candidate)
