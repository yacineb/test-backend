"""Documents, under the same row-level tenant isolation as the rest.

Revision ID: 0002_documents
Revises: 0001_auth_tenancy
Create Date: 2026-07-26

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_documents"
down_revision: str | None = "0001_auth_tenancy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Same expression as 0001: NULLIF so an unset *or empty* setting yields NULL
# rather than a cast error, and the predicate evaluates false. Default deny.
_CURRENT_ORG = "(NULLIF(current_setting('app.current_org_id', true), ''))::uuid"


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("uploaded_by", sa.Uuid(), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_documents_org_id_organizations",
            ondelete="CASCADE",
        ),
        # No ondelete: documents outlive their uploader, so deleting a user who
        # still has documents should fail rather than destroy org records.
        sa.ForeignKeyConstraint(
            ["uploaded_by"], ["users.id"], name="fk_documents_uploaded_by_users"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_documents"),
        sa.UniqueConstraint("storage_key", name="uq_documents_storage_key"),
    )
    op.create_index(
        "ix_documents_org_id_created_at", "documents", ["org_id", "created_at"]
    )

    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON documents TO app_rw, app_auth")
    op.execute("ALTER TABLE documents ENABLE ROW LEVEL SECURITY")
    # FORCE so the table owner is constrained too, should ownership ever move
    # to a non-superuser role.
    op.execute("ALTER TABLE documents FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY org_isolation ON documents
            FOR ALL
            USING (org_id = {_CURRENT_ORG})
            WITH CHECK (org_id = {_CURRENT_ORG})
    """)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS org_isolation ON documents")
    op.execute("ALTER TABLE documents DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_documents_org_id_created_at", table_name="documents")
    op.drop_table("documents")
