"""Store an uploaded file and record it against the caller's organization."""

import hashlib
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from uuid import uuid4

from app.domain.auth import AuthContext
from app.domain.document import Document, DocumentStatus
from app.domain.errors import (
    EmptyUpload,
    MissingFilename,
    UnsupportedFileType,
    UploadTooLarge,
)
from app.domain.ports import (
    Clock,
    ContentTypeDetector,
    DocumentRepository,
    ObjectStore,
    PipelineRunner,
    UnitOfWork,
)

# The corpus is PDFs; nothing else is accepted. A single constant rather than a
# config knob, because "which formats do we ingest" is a product decision the
# pipeline downstream depends on, not a per-deployment setting.
ACCEPTED_MIME = "application/pdf"

# Every magic signature puremagic knows sits far inside this. Held in memory
# only until the type is decided.
SNIFF_BYTES = 4096

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class UploadDeps:
    documents: DocumentRepository
    store: ObjectStore
    detector: ContentTypeDetector
    clock: Clock
    max_bytes: int
    # Processing starts as the upload finishes, so the use case that owns "the
    # upload is complete" is also the one that says so.
    uow: UnitOfWork
    pipeline: PipelineRunner


async def _split_head(
    chunks: AsyncIterator[bytes], size: int
) -> tuple[bytes, AsyncIterator[bytes]]:
    """Pull the first `size` bytes off a stream without losing them.

    Returns the head, plus an iterator that replays it followed by the rest, so
    the content check can run before a single byte reaches storage.
    """
    head = bytearray()
    buffered: list[bytes] = []
    async for chunk in chunks:
        buffered.append(chunk)
        head.extend(chunk)
        if len(head) >= size:
            break

    async def replayed() -> AsyncIterator[bytes]:
        for chunk in buffered:
            yield chunk
        async for chunk in chunks:
            yield chunk

    return bytes(head[:size]), replayed()


class MeasuredStream:
    """Counts and hashes an upload in a single pass, refusing to exceed `limit`.

    Size and digest come from the same traversal as the write, so integrity
    costs no extra pass over the data.
    """

    def __init__(self, chunks: AsyncIterator[bytes], limit: int) -> None:
        self._chunks = chunks
        self._limit = limit
        self._digest = hashlib.sha256()
        self.size = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._chunks:
            self.size += len(chunk)
            if self.size > self._limit:
                raise UploadTooLarge(self._limit)
            self._digest.update(chunk)
            yield chunk

    @property
    def sha256(self) -> str:
        return self._digest.hexdigest()


async def upload_document(
    deps: UploadDeps,
    ctx: AuthContext,
    *,
    filename: str | None,
    declared_size: int | None,
    chunks: AsyncIterator[bytes],
) -> Document:
    """Commit the bytes, then record the row.

    Every rule about what makes an upload acceptable lives here rather than in
    the router, so there is one owner and every entry point gets the same
    answers -- the presigned-upload completion callback in
    docs/upload-architecture.md is a different transport that will call
    straight into this.

    `declared_size` is what the multipart parser already counted while
    buffering the body; when it is available, rejecting on it avoids writing
    bytes that are about to be thrown away. MeasuredStream is the authority
    either way, and is what still holds if the transport ever becomes genuinely
    streamed and the size is unknown up front.

    The file's type is decided by its leading bytes, never by the Content-Type
    the client sent -- that header is a claim, and the whole point of the check
    is that the claim may be a lie. The sniffed type is what gets recorded.
    """
    if not filename:
        raise MissingFilename

    if declared_size is not None:
        if declared_size > deps.max_bytes:
            raise UploadTooLarge(deps.max_bytes)
        if declared_size == 0:
            raise EmptyUpload

    head, body = await _split_head(chunks, SNIFF_BYTES)
    if not head:
        raise EmptyUpload

    detected = deps.detector.sniff(head)
    if detected != ACCEPTED_MIME:
        raise UnsupportedFileType(detected)

    document_id = uuid4()
    # Server-generated. Nothing the client sent appears in this string, and the
    # org prefix is what later carries per-tenant lifecycle rules on S3.
    storage_key = f"{ctx.org_id}/{document_id}"

    stream = MeasuredStream(body, deps.max_bytes)
    await deps.store.put(storage_key, stream)

    document = Document(
        id=document_id,
        org_id=ctx.org_id,
        uploaded_by=ctx.user_id,
        filename=filename,
        content_type=detected,
        size_bytes=stream.size,
        sha256=stream.sha256,
        storage_key=storage_key,
        status=DocumentStatus.UPLOADED,
        created_at=deps.clock.now(),
    )

    try:
        await deps.documents.add(document)
    except Exception:
        # The object is committed but the row is not, so it is unreachable.
        # add() flushes, so constraint and RLS violations land here rather than
        # at request teardown, which is what makes this cleanup reachable at
        # all. A crash between the two still leaks an object; that is what the
        # orphan sweeper in docs/upload-architecture.md is for.
        #
        # ERROR because bytes were durably written and are now unreferenced.
        # The key is the whole point of the line: it is what an operator needs
        # to find the orphan if the delete below also fails.
        logger.exception(
            "upload.row_failed", extra={"storage_key": storage_key, "orphaned": True}
        )
        await deps.store.delete(storage_key)
        raise

    logger.info(
        "upload.stored",
        extra={
            "document_id": str(document.id),
            "size_bytes": document.size_bytes,
            # Enough to correlate two uploads of the same file, or to check a
            # stored object against its row, without the full digest.
            "sha256_prefix": document.sha256[:12],
            "content_type": detected,
        },
    )

    # The upload is now finished, and this is the line that says so: the
    # document and its four step rows are committed and visible before the
    # pipeline is enqueued. Enqueueing first is a race the worker usually wins,
    # because it is not waiting on an HTTP client - it would pick up a job and
    # find nothing to write to.
    await deps.uow.commit()

    workflow_id = await deps.pipeline.start(document.id, ctx.org_id)
    await deps.documents.set_workflow_id(document.id, workflow_id)
    await deps.uow.commit()

    # The hand-off from request to worker. This is the line that lets someone
    # holding a request_id follow the work into the pipeline, where there is no
    # longer a request to correlate against -- workflow_id takes over from here.
    logger.info(
        "pipeline.enqueued",
        extra={"document_id": str(document.id), "workflow_id": workflow_id},
    )

    # Re-read so the caller is handed what is actually stored - the workflow id
    # and the four pending step rows the repository wrote - rather than the
    # value this function happened to build.
    return await deps.documents.get(document.id) or replace(
        document, workflow_id=workflow_id
    )
