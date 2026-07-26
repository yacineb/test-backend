"""Notify listeners whenever a document's processing state changes.

Revision ID: 0005_progress_notify
Revises: 0004_documents_keyset_index
Create Date: 2026-07-26

The trigger, rather than application code, is what makes the guarantee cheap:

* `NOTIFY` is transactional. It is delivered when the transaction commits and
  never for a write that rolls back, so a listener cannot be told about a row
  it would not be able to read.
* It covers every write path by construction - including the partner webhook,
  which sets `ready` through a different repository on the system session and
  is exactly the kind of path application-level notification forgets.

The payload is only the document id. Subscribers re-read the projection, so a
duplicated or coalesced notification is harmless and there is no 8000-byte
payload limit to design around.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005_progress_notify"
down_revision: str | None = "0004_documents_keyset_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHANNEL = "document_progress"

# The two tables spell the document id differently, so the column name is a
# trigger argument rather than there being two near-identical functions.
# to_jsonb(NEW) lets one body read either without knowing the row's shape.
_FUNCTION = f"""
CREATE OR REPLACE FUNCTION notify_document_progress() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('{CHANNEL}', to_jsonb(NEW) ->> TG_ARGV[0]);
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;
"""

_TRIGGERS = {
    "documents": "id",
    "document_steps": "document_id",
}


def upgrade() -> None:
    op.execute(_FUNCTION)
    for table, column in _TRIGGERS.items():
        # AFTER, so the row is already written; FOR EACH ROW, so inserting the
        # four step rows of a new document wakes a subscriber four times. The
        # listener coalesces, because every wake means the same thing: re-read.
        op.execute(f"""
            CREATE TRIGGER {table}_progress_notify
                AFTER INSERT OR UPDATE ON {table}
                FOR EACH ROW
                EXECUTE FUNCTION notify_document_progress('{column}')
        """)


def downgrade() -> None:
    for table in _TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_progress_notify ON {table}")
    op.execute("DROP FUNCTION IF EXISTS notify_document_progress()")
