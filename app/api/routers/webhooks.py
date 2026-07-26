"""Inbound partner notifications.

Unauthenticated by design: the partner holds no JWT. Authenticity comes from
the HMAC over the raw body and nothing else. See docs/incoming-webhook.md.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.api.deps import (
    SIGNATURE_HEADER,
    SettingsDep,
    WebhookDepsDep,
    WebhookSignerDep,
    verify_partner_signature,
)
from app.api.schemas import (
    PartnerWebhookRequest,
    WebhookAccepted,
    WebhookSignature,
)
from app.application.webhooks import receive_partner_notification
from app.domain.partner import PartnerNotification

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post(
    "/partner",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(verify_partner_signature)],
    summary="Receive the partner's asynchronous job result",
    description=(
        f"Requires `{SIGNATURE_HEADER}: <hex HMAC-SHA256 of the raw body>`. "
        "The signature covers the exact bytes sent, so reformatting the JSON "
        "invalidates it — use POST /webhooks/partner/sign to produce a body "
        "and a matching signature."
    ),
)
async def partner_webhook(
    body: PartnerWebhookRequest, deps: WebhookDepsDep
) -> WebhookAccepted:
    await receive_partner_notification(
        deps,
        PartnerNotification(
            job_id=body.job_id,
            status=body.status,
            result=body.result,
            occurred_at=body.occurred_at,
        ),
    )
    return WebhookAccepted(job_id=body.job_id)


@router.post(
    "/partner/sign",
    summary="Dev helper: sign a body so /docs can call the webhook",
    description=(
        "Signs the exact bytes you post here. Send the same body to POST "
        f"/webhooks/partner with `signature` as `{SIGNATURE_HEADER}` — "
        "unchanged, down to the whitespace.\n\n"
        "This is a forgery oracle. Set `PARTNER_WEBHOOK_SIGNING_HELPER=false` "
        "wherever the secret is real."
    ),
)
async def sign_partner_body(
    body: PartnerWebhookRequest,
    request: Request,
    signer: WebhookSignerDep,
    settings: SettingsDep,
) -> WebhookSignature:
    """Signs the raw bytes, but declares the parsed body too.

    `body` is never read: it is there so FastAPI validates the payload and
    documents an editable textarea in /docs, the same way every other route
    gets both for free. The signature still covers `request.body()` — a
    re-serialized model would force the caller to unescape our JSON string
    before pasting it back, and signing what they typed is the only workflow
    that cannot silently drift.
    """
    if not settings.partner.signing_helper_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="signing helper is disabled",
        )

    raw = await request.body()
    return WebhookSignature(signature=signer.sign(raw), body=raw.decode())
