from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class DocumentStatus(StrEnum):
    """Where a document is in its lifecycle.

    Only UPLOADED is reachable today. The pipeline states land here next, which
    is why this is a string column rather than a Postgres enum: adding a value
    to a native enum needs a migration and takes a lock.
    """

    UPLOADED = "uploaded"


@dataclass(frozen=True, slots=True)
class Document:
    id: UUID
    org_id: UUID
    uploaded_by: UUID
    # Client-supplied, and strictly data: it is displayed, never used to build
    # a path. See storage_key.
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    # Server-generated as "{org_id}/{document_id}". No client input reaches it,
    # so path traversal is impossible by construction rather than by sanitising.
    storage_key: str
    status: DocumentStatus
    created_at: datetime
