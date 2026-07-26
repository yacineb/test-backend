"""Document routes with the persistence adapters swapped for fakes.

No database here: this covers the auth wiring, status codes and the rule that
tenancy comes from the token and nowhere else. Isolation is proven against real
row-level security in tests/integration.
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_document_repository, get_token_service, get_upload_deps
from app.domain.auth import AuthContext
from app.main import app
from tests.fakes import make_token_service, make_upload_deps, pdf_bytes

ORG = uuid4()
OTHER_ORG = uuid4()
USER = uuid4()
MAX_BYTES = 1024
PDF = pdf_bytes(b"pdf bytes")


@pytest.fixture
def wiring():
    return make_upload_deps(ORG, max_bytes=MAX_BYTES)


@pytest.fixture
def client(wiring) -> Iterator[TestClient]:
    deps, documents, _ = wiring
    app.dependency_overrides[get_token_service] = make_token_service
    app.dependency_overrides[get_upload_deps] = lambda: deps
    app.dependency_overrides[get_document_repository] = lambda: documents
    yield TestClient(app)
    app.dependency_overrides.clear()


def token_for(org_id=ORG, user_id=USER) -> str:
    access, _ = make_token_service().issue_access_token(
        AuthContext(user_id=user_id, org_id=org_id), datetime.now(UTC)
    )
    return access


def auth(token: str | None = None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token or token_for()}"}


def upload(client: TestClient, content: bytes = PDF, **kwargs):
    return client.post(
        "/documents",
        headers=kwargs.pop("headers", auth()),
        files={"file": ("report.pdf", content, "application/pdf")},
        **kwargs,
    )


def test_upload_without_a_token_is_rejected(client, wiring):
    _, documents, store = wiring

    response = client.post(
        "/documents", files={"file": ("report.pdf", PDF, "application/pdf")}
    )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert documents.documents == []
    assert store.objects == {}


def test_upload_with_a_garbage_token_is_rejected(client):
    response = upload(client, headers={"Authorization": "Bearer not-a-jwt"})

    assert response.status_code == 401


def test_upload_succeeds_with_a_bearer_token(client):
    response = upload(client)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["filename"] == "report.pdf"
    assert body["size_bytes"] == len(PDF)
    assert body["status"] == "uploaded"
    # Internal addressing detail; clients must not be able to depend on it.
    assert "storage_key" not in body


def test_org_and_uploader_are_taken_from_the_token(client, wiring):
    _, documents, _ = wiring

    body = upload(client).json()

    stored = documents.documents[0]
    assert stored.org_id == ORG
    assert stored.uploaded_by == USER
    assert body["uploaded_by"] == str(USER)


def test_a_token_for_another_org_files_the_document_under_that_org(client, wiring):
    _, documents, _ = wiring

    upload(client, headers=auth(token_for(org_id=OTHER_ORG)))

    # Whatever the token says is what governs — there is no other source.
    assert documents.documents[0].org_id == OTHER_ORG


def test_org_id_supplied_by_the_client_is_ignored(client, wiring):
    _, documents, _ = wiring

    response = client.post(
        "/documents",
        headers=auth(),
        files={"file": ("report.pdf", PDF, "application/pdf")},
        data={"org_id": str(OTHER_ORG), "uploaded_by": str(uuid4())},
    )

    assert response.status_code == 201
    # The smuggled fields changed nothing: tenancy is not a request parameter.
    assert documents.documents[0].org_id == ORG
    assert documents.documents[0].uploaded_by == USER


def test_storage_key_is_org_scoped(client, wiring):
    _, documents, store = wiring

    body = upload(client).json()

    assert list(store.objects) == [f"{ORG}/{body['id']}"]


def test_exactly_at_the_size_limit_is_accepted(client):
    response = upload(client, pdf_bytes(size=MAX_BYTES))

    assert response.status_code == 201
    assert response.json()["size_bytes"] == MAX_BYTES


def test_one_byte_over_the_size_limit_is_rejected(client, wiring):
    _, documents, store = wiring

    response = upload(client, pdf_bytes(size=MAX_BYTES + 1))

    assert response.status_code == 413
    assert str(MAX_BYTES) in response.json()["detail"]
    assert documents.documents == []
    assert store.objects == {}


def test_empty_file_is_rejected(client):
    response = upload(client, b"")

    assert response.status_code == 400


@pytest.mark.parametrize(
    ("label", "content"),
    [
        ("png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 64),
        ("zip/docx", b"PK\x03\x04" + b"\x00" * 64),
        ("plain text", b"not a pdf at all, no matter what the header says"),
    ],
)
def test_non_pdf_content_is_rejected_with_415(client, wiring, label, content):
    _, documents, store = wiring

    response = upload(client, content)

    assert response.status_code == 415
    assert "application/pdf" in response.json()["detail"]
    assert documents.documents == []
    assert store.objects == {}


def test_a_lying_content_type_header_does_not_get_a_png_accepted(client, wiring):
    """The client controls the header; it does not control the bytes."""
    _, documents, store = wiring
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64

    response = client.post(
        "/documents",
        headers=auth(),
        files={"file": ("totally-a-report.pdf", png, "application/pdf")},
    )

    assert response.status_code == 415
    assert documents.documents == []
    assert store.objects == {}


def test_the_stored_type_is_the_sniffed_one_not_the_declared_one(client, wiring):
    _, documents, _ = wiring

    body = client.post(
        "/documents",
        headers=auth(),
        # A deliberately wrong but harmless declaration on genuine PDF bytes.
        files={"file": ("report.pdf", PDF, "application/octet-stream")},
    ).json()

    assert body["content_type"] == "application/pdf"
    assert documents.documents[0].content_type == "application/pdf"


def test_listing_requires_a_token(client):
    assert client.get("/documents").status_code == 401


def test_listing_returns_the_stored_documents_newest_first(client):
    for name in ("first", "second", "third"):
        client.post(
            "/documents",
            headers=auth(),
            files={"file": (f"{name}.pdf", PDF, "application/pdf")},
        )

    listed = client.get("/documents", headers=auth()).json()

    assert [d["filename"] for d in listed] == ["third.pdf", "second.pdf", "first.pdf"]


def test_upload_appears_in_the_openapi_schema_as_secured(client):
    schema = client.get("/openapi.json").json()

    assert "security" in schema["paths"]["/documents"]["post"]
