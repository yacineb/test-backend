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
from pathlib import Path
from typing import Literal

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


class PartnerWebhookSettings(BaseSettings):
    """Inbound partner notifications: shared secret, replay window, dev helper."""

    model_config = _ENV

    # Shared out-of-band with the partner. Same rule as JWT_SECRET: the default
    # exists so `docker compose up` works, not because it is a secret.
    hmac_secret: SecretStr = Field(
        default=SecretStr("dev-only-partner-secret-change-me"),
        validation_alias="PARTNER_HMAC_SECRET",
    )

    # HMAC proves authenticity, not freshness: a captured body stays valid
    # forever. occurred_at travels inside the signed payload, so refusing stale
    # notifications closes the replay window. 0 disables the check.
    tolerance_seconds: int = Field(
        default=5 * 60, validation_alias="PARTNER_WEBHOOK_TOLERANCE_SECONDS"
    )

    # The signing helper is a forgery oracle: whoever can call it can sign
    # anything. On by default so /docs is usable out of the box; turn it off
    # wherever the secret is real.
    signing_helper_enabled: bool = Field(
        default=True, validation_alias="PARTNER_WEBHOOK_SIGNING_HELPER"
    )

    @property
    def tolerance(self) -> timedelta:
        return timedelta(seconds=self.tolerance_seconds)


class StorageSettings(BaseSettings):
    """Where document content goes, and how large it may be.

    `max_upload_bytes` is configuration rather than a constant so the size
    boundary is testable exactly: tests set it to a few hundred bytes and assert
    that limit and limit+1 land on either side, instead of pushing 100MB through
    a test client or never testing the comparison at all.
    """

    model_config = _ENV

    root: Path = Field(default=Path("/data/uploads"), validation_alias="STORAGE_ROOT")

    max_upload_bytes: int = Field(
        default=100 * 1024 * 1024, validation_alias="MAX_UPLOAD_BYTES"
    )
    # Multipart boundaries and part headers travel in the request body but are
    # not part of the file, so the request-level guard sits above the file limit.
    body_overhead_bytes: int = Field(
        default=1024 * 1024, validation_alias="MAX_BODY_OVERHEAD_BYTES"
    )
    chunk_bytes: int = Field(default=1024 * 1024, validation_alias="UPLOAD_CHUNK_BYTES")

    @property
    def max_body_bytes(self) -> int:
        return self.max_upload_bytes + self.body_overhead_bytes


class PipelineSettings(BaseSettings):
    """Orchestration: where DBOS keeps its state, and how hard it retries.

    The retry numbers are the output of scripts/simulate_pipeline.py, not a
    preference. Over 200k simulated pipelines, 5 attempts with 1/2/4/8s backoff
    gives p95 56.9s against the 120s target and a 1.6% give-up rate. DBOS ships
    retries_allowed=False and max_attempts=3, which would be 14%.
    """

    model_config = _ENV

    # DBOS owns the `dbos` schema of the same database and migrates it itself.
    # A sync psycopg URL, not asyncpg: its system-database layer is synchronous,
    # unlike the application's pools.
    system_database_url: str = Field(
        default="postgresql+psycopg://postgres:postgres@db:5432/appdb",
        validation_alias="DBOS_SYSTEM_DATABASE_URL",
    )

    max_attempts: int = Field(default=5, validation_alias="PIPELINE_MAX_ATTEMPTS")
    retry_interval_seconds: float = Field(
        default=1.0, validation_alias="PIPELINE_RETRY_INTERVAL_SECONDS"
    )
    retry_backoff_rate: float = Field(
        default=2.0, validation_alias="PIPELINE_RETRY_BACKOFF_RATE"
    )

    # Each in-flight step holds a thread, because the provider mocks block. At
    # 1k documents/day that is ~0.4 concurrent steps and this number does not
    # matter; docs/orchestration/capacity.md says where it starts to.
    queue_concurrency: int = Field(
        default=50, validation_alias="PIPELINE_QUEUE_CONCURRENCY"
    )

    # How long an enqueued fan-out branch can sit before a worker notices. Pure
    # added latency, and it lands twice per document.
    queue_polling_interval_seconds: float = Field(
        default=1.0, validation_alias="PIPELINE_QUEUE_POLLING_INTERVAL_SECONDS"
    )

    # Only the head of the OCR text reaches the read model.
    ocr_preview_chars: int = Field(
        default=200, validation_alias="PIPELINE_OCR_PREVIEW_CHARS"
    )


class ProgressSettings(BaseSettings):
    """Live progress streaming.

    Latency target from the statement is "of the order of a second"; pushing on
    commit lands well inside that, so these knobs are about connection
    behaviour rather than freshness.
    """

    model_config = _ENV

    # Must match the channel migration 0004 notifies on.
    channel: str = Field(
        default="document_progress", validation_alias="PROGRESS_CHANNEL"
    )

    # An SSE comment on an idle stream, so proxies do not reap a connection
    # that is simply waiting for a slow OCR step.
    heartbeat_seconds: float = Field(
        default=15.0, validation_alias="PROGRESS_HEARTBEAT_SECONDS"
    )

    # A document can sit in awaiting_partner for hours. Capping a single
    # connection bounds accumulation; the client reconnects and is handed a
    # fresh snapshot, so nothing is lost by closing.
    max_stream_seconds: float = Field(
        default=300.0, validation_alias="PROGRESS_MAX_STREAM_SECONDS"
    )

    # SSE `retry:` - how long the client waits before reconnecting.
    retry_ms: int = Field(default=2000, validation_alias="PROGRESS_RETRY_MS")


class LoggingSettings(BaseSettings):
    """Verbosity and output shape.

    `json` by default because logs are read by machines first -- shipped,
    indexed, and queried by `request_id`. `console` is the human-readable
    alternative for a local run; see app/observability.py for what each level
    is meant to carry.
    """

    model_config = _ENV

    level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    format: Literal["json", "console"] = Field(
        default="json", validation_alias="LOG_FORMAT"
    )


class Settings(BaseSettings):
    model_config = _ENV

    db: DatabaseSettings = Field(default_factory=DatabaseSettings)
    jwt: JwtSettings = Field(default_factory=JwtSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    partner: PartnerWebhookSettings = Field(default_factory=PartnerWebhookSettings)
    pipeline: PipelineSettings = Field(default_factory=PipelineSettings)
    progress: ProgressSettings = Field(default_factory=ProgressSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)

    # Password given to every seeded user. Local convenience only, and it
    # belongs to neither section above.
    seed_password: str = Field(default="password123", validation_alias="SEED_PASSWORD")


@lru_cache
def get_settings() -> Settings:
    return Settings()
