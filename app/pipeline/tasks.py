"""DBOS steps: the provider mocks, plus the read-model projection.

Each function here is one durable step. DBOS checkpoints its return value, so a
resumed workflow never re-runs a step that already succeeded.

The steps are `async` because everything they touch is: the projection writes
go through the same async, RLS-scoped sessions the API uses. The provider mocks
are not async - they block - so they run in a worker thread. See the README,
"The blocking sleep problem".

IDs cross workflow boundaries as strings: DBOS serialises step arguments, and
str is the boring choice that always survives the round trip.
"""

import asyncio
from uuid import UUID

from dbos import DBOS

from app.application import pipeline as projection
from app.config import get_settings
from app.domain.document import Step
from app.pipeline import runtime, steps

_settings = get_settings().pipeline

# Applied identically to all four steps. The numbers are measured, not chosen -
# see scripts/simulate_pipeline.py and docs/orchestration/capacity.md.
RETRY_POLICY = {
    "retries_allowed": True,
    "max_attempts": _settings.max_attempts,
    "interval_seconds": _settings.retry_interval_seconds,
    "backoff_rate": _settings.retry_backoff_rate,
}


def _attempt() -> int:
    """Which retry is running, according to the orchestrator.

    Read rather than counted locally: DBOS already tracks this, and assigning
    its number keeps the projection idempotent when a step is replayed after a
    crash, which an increment would not be.
    """
    status = DBOS.step_status
    return (status.current_attempt or 0) + 1 if status else 1


@DBOS.step(**RETRY_POLICY)
async def ocr_task(document_id: str, org_id: str) -> str:
    transaction = runtime.transaction_for(UUID(org_id))
    doc = UUID(document_id)
    await projection.step_started(transaction, doc, Step.OCR, _attempt())
    try:
        text = await asyncio.to_thread(steps.ocr)
    except BaseException as error:
        await projection.step_attempt_failed(transaction, doc, Step.OCR, error)
        raise

    # Only a preview is projected. The full text is this step's return value and
    # lives in the DBOS checkpoint; duplicating megabytes into the read model
    # would be ~10GB/day of write amplification at the 12-month target.
    await projection.step_succeeded(
        transaction,
        doc,
        Step.OCR,
        {"chars": len(text), "preview": text[: _settings.ocr_preview_chars]},
    )
    return text


@DBOS.step(**RETRY_POLICY)
async def metadata_task(document_id: str, org_id: str, text: str) -> dict:
    transaction = runtime.transaction_for(UUID(org_id))
    doc = UUID(document_id)
    await projection.step_started(transaction, doc, Step.METADATA, _attempt())
    try:
        meta = await asyncio.to_thread(steps.metadata, text)
    except BaseException as error:
        await projection.step_attempt_failed(transaction, doc, Step.METADATA, error)
        raise

    await projection.step_succeeded(transaction, doc, Step.METADATA, meta)
    return meta


@DBOS.step(**RETRY_POLICY)
async def chunking_task(document_id: str, org_id: str, text: str) -> list[str]:
    transaction = runtime.transaction_for(UUID(org_id))
    doc = UUID(document_id)
    await projection.step_started(transaction, doc, Step.CHUNKING, _attempt())
    try:
        chunks = await asyncio.to_thread(steps.chunking, text)
    except BaseException as error:
        await projection.step_attempt_failed(transaction, doc, Step.CHUNKING, error)
        raise

    await projection.step_succeeded(
        transaction, doc, Step.CHUNKING, {"count": len(chunks)}
    )
    return chunks


@DBOS.step(**RETRY_POLICY)
async def external_call_task(
    document_id: str, org_id: str, text: str, meta: dict, chunks: list[str]
) -> str:
    transaction = runtime.transaction_for(UUID(org_id))
    doc = UUID(document_id)
    await projection.step_started(transaction, doc, Step.EXTERNAL_CALL, _attempt())
    try:
        job_id = await asyncio.to_thread(
            steps.external_call, document_id, text, meta, chunks
        )
    except BaseException as error:
        await projection.step_attempt_failed(
            transaction, doc, Step.EXTERNAL_CALL, error
        )
        raise

    await projection.external_call_succeeded(transaction, doc, job_id)
    return job_id
