import secrets

from argon2 import PasswordHasher as Argon2
from argon2.exceptions import Argon2Error


class Argon2PasswordHasher:
    """argon2id via argon2-cffi, at the library's current recommended defaults."""

    def __init__(self) -> None:
        self._argon2 = Argon2()
        # A real hash of a value nobody knows, so verify_dummy walks exactly the
        # same code path (and cost) as a genuine mismatch. Built once at startup.
        self._decoy = self._argon2.hash(secrets.token_urlsafe(32))

    def hash(self, password: str) -> str:
        return self._argon2.hash(password)

    def verify(self, password: str, password_hash: str) -> bool:
        try:
            return self._argon2.verify(password_hash, password)
        except Argon2Error:
            # Mismatch, or a stored hash we cannot parse. Both mean "no".
            return False

    def verify_dummy(self, password: str) -> None:
        self.verify(password, self._decoy)
