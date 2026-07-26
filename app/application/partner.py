"""Applying a verified partner outcome to the document that was waiting for it.

Split from the sink adapter so the decision - what a `completed` or `failed`
notification means for a document, and what a duplicate must not do - is
testable without a database.
"""

from app.domain.document import Document, DocumentStatus
from app.domain.partner import PartnerJobStatus, PartnerNotification


def outcome_status(notification: PartnerNotification) -> DocumentStatus:
    return (
        DocumentStatus.READY
        if notification.status is PartnerJobStatus.COMPLETED
        else DocumentStatus.FAILED
    )


def is_duplicate(document: Document, notification: PartnerNotification) -> bool:
    """True when this outcome has already been applied.

    Partners retry, so the same job_id arrives more than once. A document that
    has already left awaiting_partner has been decided; re-applying would flip
    a `ready` document to `failed` on a stale retry of an earlier failure.
    """
    return document.status is not DocumentStatus.AWAITING_PARTNER
