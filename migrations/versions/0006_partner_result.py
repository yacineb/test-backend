"""Keep the partner's answer instead of acknowledging it and dropping it.

Revision ID: 0006_partner_result
Revises: 0005_progress_notify
Create Date: 2026-07-26

The partner's notification carries a `result` payload and the event time it
signed. Both are the last piece of a document's extracted data, and both sat
next to `partner_job_id` on `documents` for the same reason it does: the
outcome is what decides `status`, so one UPDATE writes all three.

`jsonb`, not a typed set of columns: the shape belongs to the partner, and the
day they add a field is not a day this service should need a migration.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_partner_result"
down_revision: str | None = "0005_progress_notify"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "documents", sa.Column("partner_result", postgresql.JSONB(), nullable=True)
    )
    op.add_column(
        "documents",
        sa.Column("partner_occurred_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("documents", "partner_occurred_at")
    op.drop_column("documents", "partner_result")
