"""What a step does to the read model, without DBOS or a database."""

import pytest

from app.application.pipeline import (
    external_call_succeeded,
    pipeline_failed,
    step_attempt_failed,
    step_started,
    step_succeeded,
)
from app.domain.document import Document, DocumentStatus, Step, StepStatus
from tests.fakes import FakeDocumentTransaction, make_document

pytestmark = pytest.mark.anyio


async def seeded() -> tuple[FakeDocumentTransaction, Document]:
    transaction = FakeDocumentTransaction()
    document = make_document(transaction.repository.org_id)
    async with transaction() as documents:
        await documents.add(document)
    return transaction, await transaction.repository.get(document.id)


def step_of(document: Document, step: Step):
    return next(s for s in document.steps if s.step is step)


async def test_starting_a_step_moves_the_document_to_processing():
    transaction, document = await seeded()

    await step_started(transaction, document.id, Step.OCR, attempt=1)

    stored = await transaction.repository.get(document.id)
    assert stored.status is DocumentStatus.PROCESSING
    assert step_of(stored, Step.OCR).status is StepStatus.RUNNING


async def test_the_attempt_number_is_assigned_not_incremented():
    """The orchestrator owns the retry count. Assigning it means a step
    replayed after a crash reports the same number instead of inflating it."""
    transaction, document = await seeded()

    await step_started(transaction, document.id, Step.OCR, attempt=3)
    await step_started(transaction, document.id, Step.OCR, attempt=3)

    stored = await transaction.repository.get(document.id)
    assert step_of(stored, Step.OCR).attempts == 3


async def test_a_failed_attempt_records_the_error_without_ending_the_step():
    transaction, document = await seeded()
    await step_started(transaction, document.id, Step.OCR, attempt=1)

    await step_attempt_failed(
        transaction, document.id, Step.OCR, TimeoutError("OCR provider timeout")
    )

    step = step_of(await transaction.repository.get(document.id), Step.OCR)
    assert step.last_error == "TimeoutError: OCR provider timeout"
    # Still running: the orchestrator may retry, and only the workflow decides
    # a step is finished for good.
    assert step.status is StepStatus.RUNNING


async def test_external_call_commits_the_job_id_with_the_status():
    """A document is never awaiting_partner without the key the webhook will
    look it up by - both writes happen in one transaction."""
    transaction, document = await seeded()
    opened_before = transaction.opened

    await external_call_succeeded(transaction, document.id, "j_abc123")

    stored = await transaction.repository.get(document.id)
    assert stored.status is DocumentStatus.AWAITING_PARTNER
    assert stored.partner_job_id == "j_abc123"
    assert transaction.opened - opened_before == 1


async def test_exhausted_retries_name_the_failing_step():
    transaction, document = await seeded()
    await step_started(transaction, document.id, Step.CHUNKING, attempt=5)

    await pipeline_failed(transaction, document.id, Step.CHUNKING)

    stored = await transaction.repository.get(document.id)
    assert stored.status is DocumentStatus.FAILED
    assert stored.failed_step is Step.CHUNKING
    assert step_of(stored, Step.CHUNKING).status is StepStatus.FAILED


async def test_a_succeeded_step_clears_its_previous_error():
    transaction, document = await seeded()
    await step_started(transaction, document.id, Step.OCR, attempt=1)
    await step_attempt_failed(transaction, document.id, Step.OCR, ValueError("boom"))

    await step_succeeded(transaction, document.id, Step.OCR, {"chars": 14})

    step = step_of(await transaction.repository.get(document.id), Step.OCR)
    assert step.status is StepStatus.SUCCEEDED
    assert step.last_error is None
