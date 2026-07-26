from app.application.deps import WebhookDeps
from app.domain.errors import StaleWebhook
from app.domain.partner import PartnerNotification


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
