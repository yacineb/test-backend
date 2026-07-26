from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID


class DocumentStatus(StrEnum):
    """Where a document is in its lifecycle.

    A string column rather than a Postgres enum: adding a value to a native
    enum needs a migration and takes a lock, and this list grew once already.

    UPLOADED is the state the upload leaves behind. Everything after it belongs
    to the pipeline, except READY, which only a verified partner notification
    can reach - see app/infrastructure/partner_jobs.py.
    """

    UPLOADED = "uploaded"
    PROCESSING = "processing"
    # external_call succeeded and the partner holds the ball.
    AWAITING_PARTNER = "awaiting_partner"
    READY = "ready"
    FAILED = "failed"


class Step(StrEnum):
    """The pipeline's shape. Declaration order is reporting order."""

    OCR = "ocr"
    METADATA = "metadata"
    CHUNKING = "chunking"
    EXTERNAL_CALL = "external_call"


# Every document carries exactly these four step rows from the moment it is
# uploaded. "Not started" is a row with status=pending, never a missing row, so
# nothing downstream needs a special case for partial progress.
STEPS: tuple[Step, ...] = tuple(Step)


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class DocumentStep:
    step: Step
    status: StepStatus
    attempts: int
    last_error: str | None
    output: dict[str, Any] | None
    started_at: datetime | None
    ended_at: datetime | None


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

    # --- pipeline ---------------------------------------------------------
    # Defaulted, so constructing a freshly uploaded document says nothing about
    # a pipeline that has not started yet.

    # Correlation keys. workflow_id ties the row to the orchestrator;
    # partner_job_id is the only join the partner can offer, since it knows
    # nothing about our tenants.
    workflow_id: str | None = None
    partner_job_id: str | None = None
    # The partner's own payload, and the event time it signed. Both arrive with
    # the notification that makes the document ready, so they live next to the
    # job_id they answer rather than in a step's output: the outcome is what
    # decides `status`, and one UPDATE writes all three.
    partner_result: dict[str, Any] | None = None
    partner_occurred_at: datetime | None = None
    failed_step: Step | None = None
    steps: tuple[DocumentStep, ...] = field(default=())

    @property
    def is_terminal(self) -> bool:
        return self.status in (DocumentStatus.READY, DocumentStatus.FAILED)


@dataclass(frozen=True, slots=True)
class DocumentCursor:
    """A position in the listing, which is exactly its sort key.

    `created_at` alone is not a position: two documents can share a timestamp,
    and a cursor that cannot separate them either skips rows or repeats them.
    `id` breaks the tie, and the pair is unique because `id` is the primary key.
    """

    created_at: datetime
    id: UUID


@dataclass(frozen=True, slots=True)
class DocumentSummary:
    """The listing row: what a document index shows, and nothing else.

    Not a `Document`. The uploader's name and email live on `users`; the
    bytes-level fields (`sha256`, `storage_key`, `content_type`, `size_bytes`)
    are not on this screen; and neither are the per-step pipeline rows, which
    are a detail view's concern. `status` is the pipeline's summary of itself,
    and it is the one pipeline field a list needs.
    """

    id: UUID
    filename: str
    status: DocumentStatus
    created_at: datetime
    uploader_id: UUID
    uploader_name: str
    uploader_email: str


@dataclass(frozen=True, slots=True)
class DocumentPage:
    """One page, and where the next one starts.

    `next_cursor` is None only when the page is the last one — it is derived
    from a row that was actually fetched, so following it never lands on an
    empty page.
    """

    items: list[DocumentSummary]
    next_cursor: DocumentCursor | None
