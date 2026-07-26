"""Deciding what a partner notification means for a document."""

from datetime import UTC, datetime

import pytest

from app.application.partner import is_duplicate, outcome_status
from app.domain.document import Document, DocumentStatus
from app.domain.partner import PartnerJobStatus, PartnerNotification
from tests.fakes import make_document


def document(status: DocumentStatus) -> Document:
    return make_document(status=status, partner_job_id="j_abc123")


def notification(status: PartnerJobStatus) -> PartnerNotification:
    return PartnerNotification(
        job_id="j_abc123",
        status=status,
        result={"indexed_at": "2026-05-21T14:23:11Z"},
        occurred_at=datetime.now(UTC),
    )


@pytest.mark.parametrize(
    ("partner_status", "expected"),
    [
        (PartnerJobStatus.COMPLETED, DocumentStatus.READY),
        (PartnerJobStatus.FAILED, DocumentStatus.FAILED),
    ],
)
def test_the_partner_decides_the_terminal_status(partner_status, expected):
    assert outcome_status(notification(partner_status)) is expected


def test_a_waiting_document_is_not_a_duplicate():
    waiting = document(DocumentStatus.AWAITING_PARTNER)

    assert not is_duplicate(waiting, notification(PartnerJobStatus.COMPLETED))


@pytest.mark.parametrize("status", [DocumentStatus.READY, DocumentStatus.FAILED])
def test_an_already_decided_document_rejects_a_retry(status):
    """Partners retry. Without this, a stale retry of an earlier failure would
    flip a document that is already ready back to failed."""
    decided = document(status)

    assert is_duplicate(decided, notification(PartnerJobStatus.FAILED))
