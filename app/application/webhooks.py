import logging

from app.application.deps import WebhookDeps
from app.domain.errors import StaleWebhook
from app.domain.partner import PartnerNotification

logger = logging.getLogger(__name__)


async def receive_partner_notification(
    deps: WebhookDeps, notification: PartnerNotification
) -> None:
    """Accept a partner notification whose signature already verified.

    HMAC proves the bytes are authentic, not that they are recent, so the
    freshness window is enforced here. A tolerance of zero turns the check off.
    """
    if deps.tolerance and notification.is_stale(deps.clock.now(), deps.tolerance):
        raise StaleWebhook("occurred_at is outside the accepted window")

    await deps.sink.deliver(notification)
    # The last hop of a document's life, arriving on a connection that has no
    # request_id from us and no tenant until the sink resolves job_id. job_id is
    # the only key both sides share, so it is what makes the line joinable to
    # pipeline.awaiting_partner.
    logger.info(
        "webhook.partner.accepted",
        extra={"job_id": notification.job_id, "partner_status": notification.status},
    )
