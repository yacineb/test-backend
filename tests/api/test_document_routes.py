"""Document routes with the persistence adapters swapped for fakes.

No database here: this covers the auth wiring, status codes and the rule that
tenancy comes from the token and nowhere else. Isolation is proven against real
row-level security in tests/integration.
"""

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_document_repository, get_token_service, get_upload_deps
from app.domain.auth import AuthContext
from app.main import app
from tests.fakes import make_token_service, make_upload_deps, make_user, pdf_bytes

ORG = uuid4()
OTHER_ORG = uuid4()
USER = uuid4()
MAX_BYTES = 1024
PDF = pdf_bytes(b"pdf bytes")

# The listing joins documents to their uploader, so the fake needs the user the
# token names to exist.
UPLOADER = replace(make_user(org_id=ORG), id=USER)


@pytest.fixture
def wiring():
    return make_upload_deps(ORG, max_bytes=MAX_BYTES, uploaders=[UPLOADER])


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


def store(client: TestClient, *names: str) -> None:
    for name in names:
        response = client.post(
            "/documents",
            headers=auth(),
            files={"file": (f"{name}.pdf", PDF, "application/pdf")},
        )
        assert response.status_code == 201, response.text


def filenames(page: dict) -> list[str]:
    return [item["filename"] for item in page["items"]]


def test_listing_returns_the_stored_documents_newest_first(client):
    store(client, "first", "second", "third")

    page = client.get("/documents", headers=auth()).json()

    assert filenames(page) == ["third.pdf", "second.pdf", "first.pdf"]
    assert page["next_cursor"] is None


def test_a_listed_row_carries_the_fields_the_index_shows(client):
    store(client, "report")

    item = client.get("/documents", headers=auth()).json()["items"][0]

    assert item["filename"] == "report.pdf"
    assert item["status"] == "uploaded"
    assert item["uploaded_by"] == {
        "id": str(USER),
        "full_name": UPLOADER.full_name,
        "email": UPLOADER.email,
    }
    assert item["created_at"]
    assert item["id"]
    # Upload facts, not index facts.
    assert "sha256" not in item
    assert "storage_key" not in item


def test_paging_walks_every_document_exactly_once(client):
    store(client, *(f"doc{n}" for n in range(7)))

    seen: list[str] = []
    cursor = None
    for _ in range(10):  # Bounded so a broken cursor fails rather than hangs.
        params = {"limit": 2} | ({"cursor": cursor} if cursor else {})
        page = client.get("/documents", headers=auth(), params=params).json()
        assert len(page["items"]) <= 2
        seen += filenames(page)
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert cursor is None
    assert seen == [f"doc{n}.pdf" for n in reversed(range(7))]


def test_the_last_full_page_does_not_promise_another_one(client):
    """next_cursor comes from a row that was read, so it never leads nowhere."""
    store(client, "one", "two")

    page = client.get("/documents", headers=auth(), params={"limit": 2}).json()

    assert len(page["items"]) == 2
    assert page["next_cursor"] is None


def test_an_empty_listing_has_no_cursor(client):
    page = client.get("/documents", headers=auth()).json()

    assert page == {"items": [], "next_cursor": None}


def test_a_document_uploaded_mid_scroll_does_not_shift_the_page(client):
    """The offset failure this design exists to avoid: with OFFSET 1, inserting
    at the head would push `second` back into view and hide `first`."""
    store(client, "first", "second")
    first_page = client.get("/documents", headers=auth(), params={"limit": 1}).json()
    assert filenames(first_page) == ["second.pdf"]

    store(client, "third")

    rest = client.get(
        "/documents",
        headers=auth(),
        params={"limit": 10, "cursor": first_page["next_cursor"]},
    ).json()

    assert filenames(rest) == ["first.pdf"]


def test_a_forged_cursor_is_a_400_not_a_500(client):
    response = client.get("/documents", headers=auth(), params={"cursor": "garbage"})

    assert response.status_code == 400
    assert response.json()["detail"] == "invalid cursor"


def test_limit_is_bounded(client):
    assert (
        client.get("/documents", headers=auth(), params={"limit": 0}).status_code == 422
    )
    assert (
        client.get("/documents", headers=auth(), params={"limit": 201}).status_code
        == 422
    )


def test_offset_is_gone(client):
    """Replaced deliberately, not accidentally: a stray offset must not silently
    page from somewhere unexpected."""
    store(client, "first", "second")

    page = client.get("/documents", headers=auth(), params={"offset": 1}).json()

    assert filenames(page) == ["second.pdf", "first.pdf"]


def test_upload_appears_in_the_openapi_schema_as_secured(client):
    schema = client.get("/openapi.json").json()

    assert "security" in schema["paths"]["/documents"]["post"]
