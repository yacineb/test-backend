"""The extracted-data projection.

`GET /documents/{id}/data` is a pure reshaping of a Document: the per-step
outputs the pipeline already wrote, plus the partner's own payload. No new read
path, so the only thing worth testing is the mapping itself.
"""

from datetime import UTC, datetime
from uuid import uuid4

from app.api.schemas import to_extracted_data
from app.domain.document import Document, DocumentStatus, DocumentStep, Step, StepStatus


def succeeded(step: Step, output: dict | None) -> DocumentStep:
    now = datetime.now(UTC)
    return DocumentStep(
        step=step,
        status=StepStatus.SUCCEEDED,
        attempts=1,
        last_error=None,
        output=output,
        started_at=now,
        ended_at=now,
    )


def ready_document(**overrides) -> Document:
    document_id = uuid4()
    org_id = uuid4()
    defaults = dict(
        id=document_id,
        org_id=org_id,
        uploaded_by=uuid4(),
        filename="report.pdf",
        content_type="application/pdf",
        size_bytes=9,
        sha256="0" * 64,
        storage_key=f"{org_id}/{document_id}",
        status=DocumentStatus.READY,
        created_at=datetime.now(UTC),
        partner_job_id="j_abc123def4567890",
        partner_result={"indexed_at": "2026-05-21T14:23:11Z"},
        partner_occurred_at=datetime(2026, 5, 21, 14, 23, 11, tzinfo=UTC),
        steps=(
            succeeded(Step.OCR, {"chars": 14, "preview": "lorem ipsum..."}),
            succeeded(Step.METADATA, {"doc_type": "fake_type"}),
            succeeded(Step.CHUNKING, {"count": 3}),
            succeeded(Step.EXTERNAL_CALL, {"partner_job_id": "j_abc123def4567890"}),
        ),
    )
    return Document(**{**defaults, **overrides})


def test_each_step_output_is_returned_under_its_own_step_name():
    data = to_extracted_data(ready_document())

    assert data.ocr == {"chars": 14, "preview": "lorem ipsum..."}
    assert data.metadata == {"doc_type": "fake_type"}
    assert data.chunks == {"count": 3}


def test_the_partner_payload_is_returned_with_the_job_it_answered():
    document = ready_document()

    data = to_extracted_data(document)

    assert data.partner is not None
    assert data.partner.job_id == "j_abc123def4567890"
    assert data.partner.result == {"indexed_at": "2026-05-21T14:23:11Z"}
    assert data.partner.occurred_at == datetime(2026, 5, 21, 14, 23, 11, tzinfo=UTC)


def test_a_partner_that_sent_no_result_still_reports_the_job_it_answered():
    """`result` is optional in the partner's contract. Its absence is not the
    absence of an outcome - the document is ready either way."""
    data = to_extracted_data(ready_document(partner_result=None))

    assert data.partner is not None
    assert data.partner.result is None


def test_the_document_identifies_itself():
    document = ready_document()

    data = to_extracted_data(document)

    assert data.document_id == document.id
    assert data.status == "ready"
