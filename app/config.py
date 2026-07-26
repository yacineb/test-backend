"""Application configuration, grouped by concern.

Every knob is an env var. Defaults target the docker compose stack so that
`docker compose up` works with no .env file; JWT_SECRET is the one value you
must override outside of local development.

Each section is its own Settings class and reads the environment itself, so
`DatabaseSettings()` is usable on its own — a migration or a script does not
have to build the whole tree. Env var names are pinned with validation_alias
rather than derived from a prefix, which keeps DATABASE_URL conventional
instead of turning it into DB_URL.
"""

from datetime import timedelta
from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


class DatabaseSettings(BaseSettings):
    """Connection URLs and pooling.

    Three URLs because three roles are involved, and they are deliberately not
    interchangeable: `url` is app_rw (subject to row-level security), `auth_url`
    is app_auth (BYPASSRLS, pre-authentication lookups and seeding), and
    `migration_url` owns the schema. See migrations/versions/0001.
    """

    model_config = _ENV

    url: str = Field(
        default="postgresql+asyncpg://app_rw:app_rw@db:5432/appdb",
        validation_alias="DATABASE_URL",
    )
    auth_url: str = Field(
        default="postgresql+asyncpg://app_auth:app_auth@db:5432/appdb",
        validation_alias="AUTH_DATABASE_URL",
    )
    migration_url: str = Field(
        default="postgresql+asyncpg://postgres:postgres@db:5432/appdb",
        validation_alias="MIGRATION_DATABASE_URL",
    )

    echo: bool = Field(default=False, validation_alias="DB_ECHO")
    pool_size: int = Field(default=10, validation_alias="DB_POOL_SIZE")
    max_overflow: int = Field(default=5, validation_alias="DB_MAX_OVERFLOW")


class JwtSettings(BaseSettings):
    """Signing and token lifetimes."""

    model_config = _ENV

    # At least 32 bytes, or PyJWT warns: HS256 keys shorter than the digest
    # weaken the MAC (RFC 7518 section 3.2). Override this outside development.
    secret: SecretStr = Field(
        default=SecretStr("dev-only-secret-change-me-in-production"),
        validation_alias="JWT_SECRET",
    )
    algorithm: str = Field(default="HS256", validation_alias="JWT_ALGORITHM")
    issuer: str = Field(default="test-backend", validation_alias="JWT_ISSUER")

    # Plain seconds rather than timedelta: pydantic only coerces ISO-8601
    # strings from the environment, and JWT_ACCESS_TTL=PT6H is a worse knob
    # than JWT_ACCESS_TTL_SECONDS=21600. The unit lives in the name.
    access_ttl_seconds: int = Field(
        default=6 * 60 * 60, validation_alias="JWT_ACCESS_TTL_SECONDS"
    )
    refresh_ttl_seconds: int = Field(
        default=30 * 24 * 60 * 60, validation_alias="JWT_REFRESH_TTL_SECONDS"
    )

    @property
    def access_ttl(self) -> timedelta:
        return timedelta(seconds=self.access_ttl_seconds)

    @property
    def refresh_ttl(self) -> timedelta:
        return timedelta(seconds=self.refresh_ttl_seconds)


class Settings(BaseSettings):
    model_config = _ENV

    db: DatabaseSettings = Field(default_factory=DatabaseSettings)
    jwt: JwtSettings = Field(default_factory=JwtSettings)

    # Password given to every seeded user. Local convenience only, and it
    # belongs to neither section above.
    seed_password: str = Field(default="password123", validation_alias="SEED_PASSWORD")


@lru_cache
def get_settings() -> Settings:
    return Settings()
