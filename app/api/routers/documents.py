import asyncio
from collections.abc import AsyncIterator
from time import monotonic
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, Query, UploadFile, status
from fastapi.responses import StreamingResponse

from app.api.cursors import CursorDep, encode_cursor
from app.api.deps import (
    CurrentUser,
    DocumentRepositoryDep,
    DocumentTransactionDep,
    ProgressHubDep,
    SettingsDep,
    UploadDepsDep,
)
from app.api.schemas import (
    DocumentDetailResponse,
    DocumentPageResponse,
    DocumentResponse,
    DocumentSummaryResponse,
    UploaderResponse,
    to_detail,
)
from app.application.upload_document import upload_document
from app.config import Settings
from app.domain.document import Document, DocumentSummary
from app.domain.errors import DocumentNotFound
from app.infrastructure.progress import ProgressHub

router = APIRouter(prefix="/documents", tags=["documents"])


def _to_response(document: Document) -> DocumentResponse:
    return DocumentResponse(
        id=document.id,
        filename=document.filename,
        content_type=document.content_type,
        size_bytes=document.size_bytes,
        sha256=document.sha256,
        status=document.status.value,
        uploaded_by=document.uploaded_by,
        created_at=document.created_at,
    )


def _to_summary_response(summary: DocumentSummary) -> DocumentSummaryResponse:
    return DocumentSummaryResponse(
        id=summary.id,
        filename=summary.filename,
        status=summary.status.value,
        uploaded_by=UploaderResponse(
            id=summary.uploader_id,
            full_name=summary.uploader_name,
            email=summary.uploader_email,
        ),
        created_at=summary.created_at,
    )


async def _iter_upload(upload: UploadFile, chunk_size: int) -> AsyncIterator[bytes]:
    while chunk := await upload.read(chunk_size):
        yield chunk


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Upload a document",
    description=(
        "The organization the document belongs to and the user credited with "
        "the upload both come from the bearer token. Neither is a parameter, "
        "so a caller cannot upload into another tenant."
    ),
    responses={
        400: {"description": "Missing filename, or the file carried no bytes"},
        413: {"description": "File exceeds the configured size limit"},
        415: {"description": "The file's contents are not a PDF"},
    },
)
async def upload(
    ctx: CurrentUser,
    deps: UploadDepsDep,
    settings: SettingsDep,
    file: Annotated[UploadFile, File(description="The document to store")],
) -> DocumentResponse:
    # The router supplies facts and the use case decides; nothing is
    # pre-validated here.
    document = await upload_document(
        deps,
        ctx,
        filename=file.filename,
        # file.content_type is deliberately not forwarded: the use case decides
        # the type from the bytes, because the client's header is only a claim.
        #
        # Known up front because the multipart parser buffers the body before
        # the handler runs; see app/api/middleware.py for why that matters.
        declared_size=file.size,
        chunks=_iter_upload(file, settings.storage.chunk_bytes),
    )
    return _to_response(document)


@router.get(
    "",
    summary="List the calling organization's documents, newest first",
    description=(
        "Scoped to the org_id in the bearer token, through an RLS-scoped "
        "session. There is no parameter that widens the scope.\n\n"
        "Paging is by cursor, not offset: pass the `next_cursor` from the "
        "previous response back as `cursor`, and stop when it comes back null. "
        "An offset would have to count past every row it skips, and would drop "
        "or duplicate rows whenever a document is uploaded mid-scroll."
    ),
    responses={400: {"description": "The cursor is not one we issued"}},
)
async def list_documents(
    ctx: CurrentUser,
    documents: DocumentRepositoryDep,
    after: CursorDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> DocumentPageResponse:
    page = await documents.list_page(limit=limit, after=after)
    return DocumentPageResponse(
        items=[_to_summary_response(summary) for summary in page.items],
        next_cursor=(
            encode_cursor(page.next_cursor) if page.next_cursor is not None else None
        ),
    )


@router.get(
    "/{document_id}",
    summary="Processing status of one document",
    description=(
        "Follow the pipeline: each of the four steps reports its status, "
        "attempt count and last error. A document reaches `awaiting_partner` "
        "when the outbound call succeeds, and `ready` only once the partner's "
        "signed webhook arrives.\n\n"
        "404 for another organization's document rather than 403: confirming "
        "existence would already leak across the tenant boundary."
    ),
    responses={404: {"description": "No such document in this organization"}},
)
async def get_document(
    document_id: UUID,
    ctx: CurrentUser,
    documents: DocumentRepositoryDep,
) -> DocumentDetailResponse:
    document = await documents.get(document_id)
    if document is None:
        raise DocumentNotFound(f"no document {document_id}")
    return to_detail(document)


def _sse(event: str, event_id: int, data: str) -> str:
    return f"event: {event}\nid: {event_id}\ndata: {data}\n\n"


async def _progress_events(
    document_id: UUID,
    transaction: DocumentTransactionDep,
    hub: ProgressHub,
    settings: Settings,
) -> AsyncIterator[str]:
    """Snapshot, then one event per change, until terminal or the cap.

    Reads go through `transaction` rather than a request-scoped session: the
    response body is produced after the endpoint has returned, by which time a
    dependency-managed session is already closed.
    """
    progress = settings.progress
    deadline = monotonic() + progress.max_stream_seconds
    event_id = 0
    last_payload: str | None = None

    async with hub.subscribe(document_id) as changed:
        while True:
            async with transaction() as documents:
                document = await documents.get(document_id)
            if document is None:  # deleted, or never visible to this tenant
                return

            payload = to_detail(document).model_dump_json()
            # Two rows of one logical change notify twice; the queue coalesces
            # what it can and this drops the rest.
            if payload != last_payload:
                event_id += 1
                last_payload = payload
                yield _sse("progress", event_id, payload)

            if document.is_terminal:
                return

            remaining = deadline - monotonic()
            if remaining <= 0:
                # Closing loses nothing: the client reconnects and the first
                # thing it gets is a fresh snapshot.
                yield f"retry: {progress.retry_ms}\n\n"
                return

            try:
                await asyncio.wait_for(
                    changed.get(), timeout=min(progress.heartbeat_seconds, remaining)
                )
            except TimeoutError:
                # Idle, not finished. Keep the connection off a proxy's reaper.
                yield ": ping\n\n"


@router.get(
    "/{document_id}/events",
    summary="Live processing status (Server-Sent Events)",
    response_class=StreamingResponse,
    description=(
        "Streams `text/event-stream`. The first event is always a full "
        "snapshot, so a reconnect needs no replay, and each subsequent event "
        "carries the same body as `GET /documents/{document_id}`.\n\n"
        "Pushed from a Postgres `NOTIFY` fired on commit, so a change reaches "
        "the client in milliseconds rather than on a poll boundary.\n\n"
        "The stream ends when the document is `ready` or `failed`. Long-lived "
        "connections are capped and closed with a `retry:` hint; the client "
        "reconnects and is handed a fresh snapshot.\n\n"
        "**Swagger cannot render a stream** - it will appear to hang. Use:\n\n"
        '```\ncurl -N -H "Authorization: Bearer $TOKEN" \\\\\n'
        "  http://localhost:8000/documents/{document_id}/events\n```"
    ),
    responses={
        200: {"content": {"text/event-stream": {}}},
        404: {"description": "No such document in this organization"},
    },
)
async def stream_document_progress(
    document_id: UUID,
    ctx: CurrentUser,
    documents: DocumentRepositoryDep,
    transaction: DocumentTransactionDep,
    hub: ProgressHubDep,
    settings: SettingsDep,
) -> StreamingResponse:
    # Authorize before subscribing, so the hub only ever routes to a caller who
    # has already proved the document is theirs.
    if await documents.get(document_id) is None:
        raise DocumentNotFound(f"no document {document_id}")

    return StreamingResponse(
        _progress_events(document_id, transaction, hub, settings),
        media_type="text/event-stream",
        headers={
            # nginx buffers responses by default, which would hold events back
            # until the buffer fills - the one thing this endpoint must not do.
            "X-Accel-Buffering": "no",
        },
        # No Cache-Control here on purpose: SecurityHeadersMiddleware assigns
        # `no-store, max-age=0` to every response, which is stronger than the
        # `no-cache` an SSE endpoint would normally set for itself.
    )
