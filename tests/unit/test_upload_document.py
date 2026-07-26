import hashlib
from uuid import uuid4

import pytest

from app.application.upload_document import upload_document
from app.domain.auth import AuthContext
from app.domain.document import DocumentStatus
from app.domain.errors import EmptyUpload, MissingFilename, UploadTooLarge
from tests.fakes import chunks, make_upload_deps

pytestmark = pytest.mark.anyio


@pytest.fixture
def ctx() -> AuthContext:
    return AuthContext(user_id=uuid4(), org_id=uuid4())


async def upload(deps, ctx, content: bytes, *, filename="report.pdf", declared=...):
    return await upload_document(
        deps,
        ctx,
        filename=filename,
        content_type="application/pdf",
        declared_size=len(content) if declared is ... else declared,
        chunks=chunks(content),
    )


async def test_returns_a_document_describing_what_was_stored(ctx):
    deps, documents, store = make_upload_deps(ctx.org_id)

    document = await upload(deps, ctx, b"pdf bytes")

    assert document.size_bytes == 9
    assert document.sha256 == hashlib.sha256(b"pdf bytes").hexdigest()
    assert document.status is DocumentStatus.UPLOADED
    assert documents.documents == [document]
    assert store.objects[document.storage_key] == b"pdf bytes"


async def test_org_and_uploader_come_from_the_auth_context(ctx):
    deps, _, _ = make_upload_deps(ctx.org_id)

    document = await upload(deps, ctx, b"x")

    # Neither is a parameter of the use case; both are taken from the token.
    assert document.org_id == ctx.org_id
    assert document.uploaded_by == ctx.user_id


async def test_storage_key_is_org_scoped_and_carries_no_client_input(ctx):
    deps, _, _ = make_upload_deps(ctx.org_id)

    document = await upload(deps, ctx, b"x", filename="../../../etc/passwd")

    assert document.storage_key == f"{ctx.org_id}/{document.id}"
    # The hostile name survives as data, and touches nothing addressable.
    assert document.filename == "../../../etc/passwd"
    assert "passwd" not in document.storage_key


async def test_declared_size_over_the_limit_is_refused_before_any_write(ctx):
    deps, documents, store = make_upload_deps(ctx.org_id, max_bytes=10)

    with pytest.raises(UploadTooLarge):
        await upload(deps, ctx, b"x" * 11)

    assert store.objects == {}
    assert documents.documents == []


async def test_stream_over_the_limit_is_refused_when_size_is_unknown(ctx):
    # declared_size=None is the genuinely-streamed case, where the size cannot
    # be known up front. MeasuredStream is the check that still applies.
    deps, documents, store = make_upload_deps(ctx.org_id, max_bytes=10)

    with pytest.raises(UploadTooLarge):
        await upload(deps, ctx, b"x" * 11, declared=None)

    assert store.objects == {}
    assert documents.documents == []


async def test_exactly_at_the_limit_is_accepted(ctx):
    deps, _, _ = make_upload_deps(ctx.org_id, max_bytes=10)

    document = await upload(deps, ctx, b"x" * 10)

    assert document.size_bytes == 10


@pytest.mark.parametrize("filename", [None, ""])
async def test_a_nameless_upload_is_refused(ctx, filename):
    # Enforced in the use case, not the router, so every entry point that ever
    # calls this -- including the presigned-completion path -- gets the rule.
    deps, documents, store = make_upload_deps(ctx.org_id)

    with pytest.raises(MissingFilename):
        await upload(deps, ctx, b"x", filename=filename)

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
        await upload(deps, ctx, b"x")

    # No row, so the object is unreachable; it must not be left behind.
    assert store.objects == {}
