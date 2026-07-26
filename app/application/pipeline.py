"""What a pipeline step does to the projection, independent of the orchestrator.

Kept out of app/pipeline/ on purpose: these are the only interesting decisions
in a step wrapper, and here they can be tested against a fake transaction
without standing up DBOS or a database.

Each function is one short transaction. Steps sleep for seconds at a time, and
holding a tenant connection open across that would be a waste of the pool.
"""

from uuid import UUID

from app.domain.document import Step
from app.domain.ports import DocumentTransaction


async def step_started(
    transaction: DocumentTransaction, document_id: UUID, step: Step, attempt: int
) -> None:
    async with transaction() as documents:
        await documents.start_step(document_id, step, attempt)


async def step_succeeded(
    transaction: DocumentTransaction,
    document_id: UUID,
    step: Step,
    output: dict | None,
) -> None:
    async with transaction() as documents:
        await documents.finish_step(document_id, step, output)


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


async def pipeline_failed(
    transaction: DocumentTransaction, document_id: UUID, step: Step
) -> None:
    """A step exhausted its retries. This is where a document dies."""
    async with transaction() as documents:
        await documents.fail(document_id, step)
