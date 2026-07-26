from dataclasses import asdict
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, EmailStr, Field

from app.domain.document import Document, Step, StepStatus
from app.domain.partner import PartnerJobStatus


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class DocumentResponse(BaseModel):
    """Public view of a document.

    `storage_key` is deliberately absent: it is an internal addressing detail,
    and returning it invites clients to depend on the layout.
    """

    id: UUID
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    status: str
    uploaded_by: UUID
    created_at: datetime


class UploaderResponse(BaseModel):
    """The user who imported the document.

    Nested rather than flattened into `uploaded_by_id`/`uploaded_by_name`/…
    because it is one entity, and because a listing that returned only a UUID
    would push every client into an N+1 lookup to render a name.
    """

    id: UUID
    full_name: str
    email: str


class DocumentSummaryResponse(BaseModel):
    """A row of the document index. Deliberately narrower than
    `DocumentResponse`: content type, size and digest are upload facts, not
    things a list view shows."""

    id: UUID
    filename: str
    # Processing status. Only "uploaded" is reachable until the pipeline lands;
    # a string rather than an enum so new states do not break old clients.
    status: str
    uploaded_by: UploaderResponse
    created_at: datetime


class DocumentPageResponse(BaseModel):
    """One page of documents, newest first.

    `next_cursor` is null on the last page. It is derived from a row that was
    actually read, so it never points at an empty page — a client can loop
    until it is null without a trailing empty request.
    """

    items: list[DocumentSummaryResponse]
    next_cursor: str | None = None


class MeResponse(BaseModel):
    user_id: UUID
    org_id: UUID
    email: str
    full_name: str
    org_name: str
    org_slug: str


class PartnerWebhookRequest(BaseModel):
    """The partner's notification body. Extra keys are ignored on purpose: the
    signature covers the whole body, so a field we do not know about is the
    partner adding one, not an attack."""

    job_id: str = Field(min_length=1, examples=["j_abc123def4567890"])
    status: PartnerJobStatus
    result: dict[str, Any] | None = None
    # Aware, not plain datetime: the freshness check subtracts this from a UTC
    # now, and a naive timestamp would be a 500. Guessing a timezone would be
    # worse — it decides whether a replay is inside the window.
    occurred_at: AwareDatetime


class WebhookAccepted(BaseModel):
    job_id: str


class WebhookSignature(BaseModel):
    """A signature and the exact body it covers. Paste `body` back verbatim."""

    signature: str
    body: str


class StepView(BaseModel):
    """One pipeline step's progress."""

    step: Step
    status: StepStatus
    attempts: int
    last_error: str | None = None
    output: dict[str, Any] | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None


class DocumentDetailResponse(DocumentResponse):
    """A document plus where its pipeline has got to."""

    # Opaque to the client; it is the key the partner's webhook joins on.
    partner_job_id: str | None = None
    failed_step: Step | None = None
    steps: list[StepView]


def to_detail(document: Document) -> DocumentDetailResponse:
    return DocumentDetailResponse(
        id=document.id,
        filename=document.filename,
        content_type=document.content_type,
        size_bytes=document.size_bytes,
        sha256=document.sha256,
        status=document.status.value,
        uploaded_by=document.uploaded_by,
        created_at=document.created_at,
        partner_job_id=document.partner_job_id,
        failed_step=document.failed_step,
        steps=[StepView(**asdict(step)) for step in document.steps],
    )
