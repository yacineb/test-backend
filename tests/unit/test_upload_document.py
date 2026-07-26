import hashlib
from uuid import uuid4

import pytest

from app.application.upload_document import upload_document
from app.domain.auth import AuthContext
from app.domain.document import DocumentStatus
from app.domain.errors import (
    EmptyUpload,
    MissingFilename,
    UnsupportedFileType,
    UploadTooLarge,
)
from tests.fakes import chunks, make_upload_deps, pdf_bytes

pytestmark = pytest.mark.anyio

PDF = pdf_bytes(b"pdf bytes")
LIMIT = 64


@pytest.fixture
def ctx() -> AuthContext:
    return AuthContext(user_id=uuid4(), org_id=uuid4())


async def upload(
    deps, ctx, content: bytes = PDF, *, filename="report.pdf", declared=...
):
    return await upload_document(
        deps,
        ctx,
        filename=filename,
        declared_size=len(content) if declared is ... else declared,
        chunks=chunks(content),
    )


async def test_returns_a_document_describing_what_was_stored(ctx):
    deps, documents, store = make_upload_deps(ctx.org_id)

    document = await upload(deps, ctx)

    assert document.size_bytes == len(PDF)
    assert document.sha256 == hashlib.sha256(PDF).hexdigest()
    assert document.status is DocumentStatus.UPLOADED
    assert documents.documents == [document]
    assert store.objects[document.storage_key] == PDF


async def test_org_and_uploader_come_from_the_auth_context(ctx):
    deps, _, _ = make_upload_deps(ctx.org_id)

    document = await upload(deps, ctx)

    # Neither is a parameter of the use case; both are taken from the token.
    assert document.org_id == ctx.org_id
    assert document.uploaded_by == ctx.user_id


async def test_storage_key_is_org_scoped_and_carries_no_client_input(ctx):
    deps, _, _ = make_upload_deps(ctx.org_id)

    document = await upload(deps, ctx, filename="../../../etc/passwd")

    assert document.storage_key == f"{ctx.org_id}/{document.id}"
    # The hostile name survives as data, and touches nothing addressable.
    assert document.filename == "../../../etc/passwd"
    assert "passwd" not in document.storage_key


@pytest.mark.parametrize(
    ("label", "content"),
    [
        ("png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 64),
        ("zip/docx", b"PK\x03\x04" + b"\x00" * 64),
        ("html", b"<!DOCTYPE html><html><body>hi</body></html>"),
        ("plain text", b"this is definitely not a pdf, whatever anyone says"),
        ("pdf marker too late", b"leading junk then %PDF-1.7"),
    ],
)
async def test_non_pdf_content_is_refused(ctx, label, content):
    deps, documents, store = make_upload_deps(ctx.org_id, max_bytes=4096)

    with pytest.raises(UnsupportedFileType):
        await upload(deps, ctx, content)

    # Rejected before anything was written, not cleaned up afterwards.
    assert store.objects == {}
    assert documents.documents == []


async def test_the_recorded_type_is_sniffed_not_claimed(ctx):
    """The use case takes no content_type argument at all.

    A client that labels a PNG as application/pdf has no way to influence what
    gets recorded, because the header never reaches this layer.
    """
    deps, _, _ = make_upload_deps(ctx.org_id)

    document = await upload(deps, ctx)

    assert document.content_type == "application/pdf"


async def test_declared_size_over_the_limit_is_refused_before_any_write(ctx):
    deps, documents, store = make_upload_deps(ctx.org_id, max_bytes=LIMIT)

    with pytest.raises(UploadTooLarge):
        await upload(deps, ctx, pdf_bytes(size=LIMIT + 1))

    assert store.objects == {}
    assert documents.documents == []


async def test_stream_over_the_limit_is_refused_when_size_is_unknown(ctx):
    # declared_size=None is the genuinely-streamed case, where the size cannot
    # be known up front. MeasuredStream is the check that still applies.
    deps, documents, store = make_upload_deps(ctx.org_id, max_bytes=LIMIT)

    with pytest.raises(UploadTooLarge):
        await upload(deps, ctx, pdf_bytes(size=LIMIT + 1), declared=None)

    assert store.objects == {}
    assert documents.documents == []


async def test_exactly_at_the_limit_is_accepted(ctx):
    deps, _, _ = make_upload_deps(ctx.org_id, max_bytes=LIMIT)

    document = await upload(deps, ctx, pdf_bytes(size=LIMIT))

    assert document.size_bytes == LIMIT


@pytest.mark.parametrize("filename", [None, ""])
async def test_a_nameless_upload_is_refused(ctx, filename):
    # Enforced in the use case, not the router, so every entry point that ever
    # calls this -- including the presigned-completion path -- gets the rule.
    deps, documents, store = make_upload_deps(ctx.org_id)

    with pytest.raises(MissingFilename):
        await upload(deps, ctx, filename=filename)

    assert store.objects == {}
    assert documents.documents == []


async def test_empty_declared_size_is_refused(ctx):
    deps, _, store = make_upload_deps(ctx.org_id)

    with pytest.raises(EmptyUpload):
        await upload(deps, ctx, b"")

    assert store.objects == {}


async def test_empty_stream_is_refused_and_leaves_nothing_stored(ctx):
    deps, documents, store = make_upload_deps(ctx.org_id)

    with pytest.raises(EmptyUpload):
        await upload(deps, ctx, b"", declared=None)

    assert store.objects == {}
    assert documents.documents == []


async def test_a_failed_insert_removes_the_object_it_would_have_referenced(ctx):
    deps, documents, store = make_upload_deps(ctx.org_id)
    documents.fail_with = RuntimeError("constraint violation")

    with pytest.raises(RuntimeError, match="constraint violation"):
        await upload(deps, ctx)

    # No row, so the object is unreachable; it must not be left behind.
    assert store.objects == {}


async def test_the_pipeline_is_triggered_when_the_upload_finishes(ctx):
    """Processing starts as the upload completes, and not one instant earlier.

    The document and its step rows must be committed before the job is
    enqueued: a worker is not waiting on an HTTP client, so it wins that race
    and finds nothing to write to.
    """
    deps, documents, _ = make_upload_deps(ctx.org_id)

    document = await upload(deps, ctx)

    runner = deps.pipeline
    assert runner.started == [(document.id, ctx.org_id)]
    # A commit had already happened by the time start() was called.
    assert runner.commits_before_start == 1


async def test_the_workflow_id_is_recorded_on_the_document(ctx):
    deps, _, _ = make_upload_deps(ctx.org_id)

    document = await upload(deps, ctx)

    assert document.workflow_id == deps.pipeline.workflow_id


async def test_a_failed_upload_never_starts_a_pipeline(ctx):
    """The row is not there, so there is nothing for a worker to process."""
    deps, documents, store = make_upload_deps(ctx.org_id)
    documents.fail_with = RuntimeError("constraint violation")

    with pytest.raises(RuntimeError):
        await upload(deps, ctx)

    assert deps.pipeline.started == []
    assert store.objects == {}


async def test_a_pdf_larger_than_the_sniff_window_is_stored_whole(ctx):
    """The head is peeked, then replayed -- nothing may be dropped or reordered."""
    big = pdf_bytes(b"x" * 20_000)
    deps, _, store = make_upload_deps(ctx.org_id, max_bytes=100_000)

    document = await upload(deps, ctx, big)

    assert store.objects[document.storage_key] == big
    assert document.sha256 == hashlib.sha256(big).hexdigest()
