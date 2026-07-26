"""What a pipeline step does to the projection, independent of the orchestrator.

Kept out of app/pipeline/ on purpose: these are the only interesting decisions
in a step wrapper, and here they can be tested against a fake transaction
without standing up DBOS or a database.

Each function is one short transaction. Steps sleep for seconds at a time, and
holding a tenant connection open across that would be a waste of the pool.

These six functions are also the whole observable life of a document, so the
step logging lives here rather than in the four near-identical wrappers in
app/pipeline/tasks.py -- one place to change, and no way for a new step to be
added without it.

There is no request behind a worker, so `workflow_id` is the correlation key
from here on; `pipeline.enqueued` in upload_document ties it back to the
request that started it.
"""

import logging
from uuid import UUID

from app.domain.document import Step
from app.domain.ports import DocumentTransaction

logger = logging.getLogger(__name__)


async def step_started(
    transaction: DocumentTransaction, document_id: UUID, step: Step, attempt: int
) -> None:
    async with transaction() as documents:
        await documents.start_step(document_id, step, attempt)
    logger.info(
        "pipeline.step.started",
        extra={"document_id": str(document_id), "step": step.value, "attempt": attempt},
    )


async def step_succeeded(
    transaction: DocumentTransaction,
    document_id: UUID,
    step: Step,
    output: dict | None,
) -> None:
    async with transaction() as documents:
        await documents.finish_step(document_id, step, output)
    logger.info(
        "pipeline.step.succeeded",
        extra={"document_id": str(document_id), "step": step.value},
    )


async def step_attempt_failed(
    transaction: DocumentTransaction,
    document_id: UUID,
    step: Step,
    error: BaseException,
) -> None:
    """One attempt failed. Not terminal - the orchestrator may still retry."""
    async with transaction() as documents:
        await documents.record_step_error(
            document_id, step, f"{type(error).__name__}: {error}"
        )
    # WARNING, not ERROR: the providers fail a third of the time by design and
    # the orchestrator will retry. Only exhausting the retries is a failure.
    logger.warning(
        "pipeline.step.attempt_failed",
        extra={
            "document_id": str(document_id),
            "step": step.value,
            "error": f"{type(error).__name__}: {error}",
        },
    )


async def external_call_succeeded(
    transaction: DocumentTransaction, document_id: UUID, job_id: str
) -> None:
    """One transaction for the step result and the correlation key.

    The partner may call back the instant external_call returns, so
    partner_job_id must never be invisible while the status invites the lookup.
    """
    async with transaction() as documents:
        await documents.finish_step(
            document_id, Step.EXTERNAL_CALL, {"partner_job_id": job_id}
        )
        await documents.await_partner(document_id, job_id)
    # partner_job_id is the only key the partner will quote when it calls back,
    # so this line is what connects an inbound webhook to a document.
    logger.info(
        "pipeline.awaiting_partner",
        extra={"document_id": str(document_id), "partner_job_id": job_id},
    )


async def pipeline_failed(
    transaction: DocumentTransaction, document_id: UUID, step: Step
) -> None:
    """A step exhausted its retries. This is where a document dies."""
    async with transaction() as documents:
        await documents.fail(document_id, step)
    # Terminal: retries are spent and this document will not finish on its own.
    logger.error(
        "pipeline.failed",
        extra={"document_id": str(document_id), "step": step.value},
    )
