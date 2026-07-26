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
