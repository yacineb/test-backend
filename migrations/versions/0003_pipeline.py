"""Pipeline state: step projection and partner correlation.

Revision ID: 0003_pipeline
Revises: 0002_documents
Create Date: 2026-07-26

0002 created `documents` for the upload path, whose only status is `uploaded`.
This adds what the processing pipeline needs on top: three correlation columns
on `documents`, and the `document_steps` table that carries per-step progress.

DBOS keeps its own tables in the `dbos` schema and migrates them itself on
launch; nothing here touches them. `document_steps` is the tenant-facing
projection, so it gets the same row-level security treatment as everything in
0001 and 0002.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_pipeline"
down_revision: str | None = "0002_documents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CURRENT_ORG = "(NULLIF(current_setting('app.current_org_id', true), ''))::uuid"


def upgrade() -> None:
    op.add_column("documents", sa.Column("workflow_id", sa.String(128), nullable=True))
    op.add_column(
        "documents", sa.Column("partner_job_id", sa.String(128), nullable=True)
    )
    op.add_column("documents", sa.Column("failed_step", sa.String(32), nullable=True))
    op.create_unique_constraint(
        "uq_documents_workflow_id", "documents", ["workflow_id"]
    )
    # Unique because a partner notification must never be applicable to two
    # documents, and it is the only key the partner can offer.
    op.create_unique_constraint(
        "uq_documents_partner_job_id", "documents", ["partner_job_id"]
    )

    op.create_table(
        "document_steps",
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("step", sa.String(length=32), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "attempts", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("output", postgresql.JSONB(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name="fk_document_steps_document_id_documents",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_document_steps_org_id_organizations",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("document_id", "step", name="pk_document_steps"),
    )
    op.create_index("ix_document_steps_org_id", "document_steps", ["org_id"])

    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON document_steps TO app_rw, app_auth"
    )
    op.execute("ALTER TABLE document_steps ENABLE ROW LEVEL SECURITY")
    # FORCE so the table owner is constrained too, should ownership ever move
    # to a non-superuser role.
    op.execute("ALTER TABLE document_steps FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY org_isolation ON document_steps
            FOR ALL
            USING (org_id = {_CURRENT_ORG})
            WITH CHECK (org_id = {_CURRENT_ORG})
    """)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS org_isolation ON document_steps")
    op.execute("ALTER TABLE document_steps DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_document_steps_org_id", table_name="document_steps")
    op.drop_table("document_steps")

    op.drop_constraint("uq_documents_partner_job_id", "documents", type_="unique")
    op.drop_constraint("uq_documents_workflow_id", "documents", type_="unique")
    op.drop_column("documents", "failed_step")
    op.drop_column("documents", "partner_job_id")
    op.drop_column("documents", "workflow_id")
