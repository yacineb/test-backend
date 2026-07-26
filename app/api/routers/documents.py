from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, File, Query, UploadFile, status

from app.api.deps import CurrentUser, DocumentRepositoryDep, SettingsDep, UploadDepsDep
from app.api.schemas import DocumentResponse
from app.application.upload_document import upload_document
from app.domain.document import Document

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
        content_type=file.content_type or "application/octet-stream",
        # Known up front because the multipart parser buffers the body before
        # the handler runs; see app/api/middleware.py for why that matters.
        declared_size=file.size,
        chunks=_iter_upload(file, settings.storage.chunk_bytes),
    )
    return _to_response(document)


@router.get(
    "",
    summary="List the calling organization's documents",
    description=(
        "Scoped to the org_id in the bearer token, through an RLS-scoped "
        "session. There is no parameter that widens the scope."
    ),
)
async def list_documents(
    ctx: CurrentUser,
    documents: DocumentRepositoryDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[DocumentResponse]:
    found = await documents.list_recent(limit=limit, offset=offset)
    return [_to_response(document) for document in found]
