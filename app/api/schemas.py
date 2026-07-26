from typing import Any
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, EmailStr, Field

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
