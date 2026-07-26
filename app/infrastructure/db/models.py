"""SQLAlchemy tables.

Kept separate from the domain dataclasses on purpose: the ORM row is a
persistence detail, and the domain must not grow a dependency on SQLAlchemy.
Repositories translate between the two.
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.db.base import Base


class OrganizationRow(Base):
    __tablename__ = "organizations"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class UserRow(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    org_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    # Globally unique: login is (email, password) with no org selector, so one
    # person cannot hold accounts in two organizations.
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("ix_users_org_id", "org_id"),)


class RefreshTokenRow(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    org_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    family_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    # SHA-256 hex of the secret handed to the client. The secret itself is never
    # stored, so a database leak does not yield usable refresh tokens.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("ix_refresh_tokens_family_id", "family_id"),
        Index("ix_refresh_tokens_user_id", "user_id"),
    )


class DocumentRow(Base):
    __tablename__ = "documents"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    org_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    # No ondelete: documents outlive the person who uploaded them, so removing
    # a user who still has documents should fail loudly rather than silently
    # destroy org records. There is no user-deletion path yet; this makes the
    # decision explicit when one arrives.
    uploaded_by: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id"), nullable=False
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # --- pipeline -------------------------------------------------------
    workflow_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    # The partner's only join key. Unique so a duplicate notification can never
    # resolve to two documents.
    partner_job_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    # The partner's payload, kept as it arrived: its shape is theirs.
    partner_result: Mapped[dict | None] = mapped_column(JSONB)
    # From inside the signed body, so it is the partner's event time rather
    # than the moment we happened to receive it.
    partner_occurred_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    failed_step: Mapped[str | None] = mapped_column(String(32))

    # Serves the only list query there is: one org's documents, newest first.
    # `id` is the third column because it is half of the keyset cursor — with
    # only (org_id, created_at) the seek predicate on (created_at, id) has to
    # recheck ties against the heap. Ascending is fine for a DESC listing;
    # Postgres scans a btree backwards.
    __table_args__ = (
        Index("ix_documents_org_id_created_at_id", "org_id", "created_at", "id"),
    )


class DocumentStepRow(Base):
    __tablename__ = "document_steps"

    document_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True
    )
    step: Mapped[str] = mapped_column(String(32), primary_key=True)

    # Denormalized from documents so this table can carry its own RLS policy.
    # Every other tenant table keys its policy on a column it owns; a policy
    # that had to join back to documents would be both slower and a special
    # case in migrations/versions/0003.
    org_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )

    status: Mapped[str] = mapped_column(String(16), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)

    # Always jsonb, never a special case: a payload too big to inline becomes a
    # pointer inside the same column rather than a different schema.
    output: Mapped[dict | None] = mapped_column(JSONB)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_document_steps_org_id", "org_id"),)
