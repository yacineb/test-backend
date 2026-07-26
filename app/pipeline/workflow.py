"""The pipeline itself.

    ocr -> (metadata || chunking) -> external_call -> awaiting_partner

The workflow *ends* at awaiting_partner. It does not park on DBOS.recv()
waiting for the partner: holding workflow state open for hours against a third
party buys nothing that a status column and a correlation key do not, and it
keeps the inbound webhook independently testable. POST /webhooks/partner
resolves the job_id and finishes the document.
"""

from uuid import UUID

from dbos import DBOS, Queue, SetEnqueueOptions

from app.application import pipeline as projection
from app.config import get_settings
from app.domain.document import Step
from app.pipeline import runtime
from app.pipeline.tasks import (
    chunking_task,
    external_call_task,
    metadata_task,
    ocr_task,
)

_settings = get_settings().pipeline

# partition_queue enforces concurrency per partition key, and the key is the
# organization. One tenant uploading 10k documents cannot starve another
# tenant's single upload; without it, fairness is a queue-ordering accident.
pipeline_queue = Queue(
    "pipeline",
    concurrency=_settings.queue_concurrency,
    partition_queue=True,
    polling_interval_sec=_settings.queue_polling_interval_seconds,
)


# The fan-out branches. Queue.enqueue takes a workflow, so each parallel leg is
# a one-line child workflow around its step. That is what buys real concurrency
# rather than two sequential awaits.


@DBOS.workflow()
async def metadata_branch(document_id: str, org_id: str, text: str) -> dict:
    return await metadata_task(document_id, org_id, text)


@DBOS.workflow()
async def chunking_branch(document_id: str, org_id: str, text: str) -> list[str]:
    return await chunking_task(document_id, org_id, text)


@DBOS.workflow()
async def process_document(document_id: str, org_id: str) -> str:
    """Returns the partner job_id. Raises if any step exhausts its retries."""
    doc = UUID(document_id)
    transaction = runtime.transaction_for(UUID(org_id))

    try:
        text = await ocr_task(document_id, org_id)
    except Exception:
        await projection.pipeline_failed(transaction, doc, Step.OCR)
        raise

    with SetEnqueueOptions(queue_partition_key=org_id):
        handles = {
            Step.METADATA: await pipeline_queue.enqueue_async(
                metadata_branch, document_id, org_id, text
            ),
            Step.CHUNKING: await pipeline_queue.enqueue_async(
                chunking_branch, document_id, org_id, text
            ),
        }

    # Collect both before deciding, so a failing branch never orphans a running
    # sibling. Iteration order of `handles` decides which failure we report.
    results: dict[Step, object] = {}
    failure: tuple[Step, Exception] | None = None
    for step, handle in handles.items():
        try:
            results[step] = await handle.get_result()
        except Exception as error:
            failure = failure or (step, error)

    if failure is not None:
        step, error = failure
        await projection.pipeline_failed(transaction, doc, step)
        raise error

    try:
        return await external_call_task(
            document_id, org_id, text, results[Step.METADATA], results[Step.CHUNKING]
        )
    except Exception:
        await projection.pipeline_failed(transaction, doc, Step.EXTERNAL_CALL)
        raise


class DbosPipelineRunner:
    """The PipelineRunner port, backed by DBOS.

    Starting a workflow is all this does. Persisting the returned id is the
    caller's business, which keeps the use case in charge of its transactions.
    """

    async def start(self, document_id: UUID, org_id: UUID) -> str:
        handle = await DBOS.start_workflow_async(
            process_document, str(document_id), str(org_id)
        )
        return handle.get_workflow_id()
