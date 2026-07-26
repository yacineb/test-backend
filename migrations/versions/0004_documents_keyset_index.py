"""Widen the documents list index to cover the keyset cursor.

The listing pages on (created_at DESC, id DESC). With only (org_id, created_at)
the index locates the starting timestamp but cannot resolve the id half of the
seek predicate, so ties are rechecked against the heap. Appending `id` makes the
whole predicate and the whole ordering index-resolvable.

Revision ID: 0004_documents_keyset_index
Revises: 0003_pipeline
Create Date: 2026-07-26

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004_documents_keyset_index"
down_revision: str | None = "0003_pipeline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Create before drop: the table is never left without an index serving the
    # list query, which is the one query that would otherwise seq-scan.
    op.create_index(
        "ix_documents_org_id_created_at_id",
        "documents",
        ["org_id", "created_at", "id"],
    )
    op.drop_index("ix_documents_org_id_created_at", table_name="documents")


def downgrade() -> None:
    op.create_index(
        "ix_documents_org_id_created_at", "documents", ["org_id", "created_at"]
    )
    op.drop_index("ix_documents_org_id_created_at_id", table_name="documents")
