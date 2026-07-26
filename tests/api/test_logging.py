"""Correlation, verbosity, and what must never reach a log line."""

import io
import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import Headers

from app.api.deps import get_document_repository, get_token_service, get_upload_deps
from app.api.request_context import REQUEST_ID_HEADER, _incoming_request_id
from app.domain.auth import AuthContext
from app.main import app
from app.observability import build_formatter
from tests.fakes import make_token_service, make_upload_deps, pdf_bytes

ORG = uuid4()
USER = uuid4()
PDF = pdf_bytes(b"pdf bytes")


@pytest.fixture
def client() -> Iterator[TestClient]:
    deps, documents, _ = make_upload_deps(ORG, max_bytes=4096)
    app.dependency_overrides[get_token_service] = make_token_service
    app.dependency_overrides[get_upload_deps] = lambda: deps
    app.dependency_overrides[get_document_repository] = lambda: documents
    yield TestClient(app)
    app.dependency_overrides.clear()


def token() -> str:
    access, _ = make_token_service().issue_access_token(
        AuthContext(user_id=USER, org_id=ORG), datetime.now(UTC)
    )
    return access


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {token()}"}


# --- correlation ------------------------------------------------------------


def test_every_response_carries_a_request_id(client):
    response = client.get("/health")

    assert response.headers[REQUEST_ID_HEADER]


def test_a_caller_supplied_request_id_is_honoured(client):
    response = client.get("/health", headers={REQUEST_ID_HEADER: "trace-abc-123"})

    # Lets a gateway or client correlate across service boundaries.
    assert response.headers[REQUEST_ID_HEADER] == "trace-abc-123"


@pytest.mark.parametrize(
    "hostile",
    [
        "bad\nid",  # newline: forges a second log line
        "bad id",  # whitespace
        'x" "injected',  # quote: forges a JSON field
        "x" * 65,  # unbounded
        "",  # empty
    ],
)
def test_a_hostile_request_id_is_replaced_not_echoed(hostile):
    """This value lands in every log line for the request, so it is untrusted
    input reaching log output -- the classic log-injection path."""
    generated = _incoming_request_id(Headers({REQUEST_ID_HEADER: hostile}))

    assert generated != hostile
    assert len(generated) == 32 and generated.isalnum()


def test_two_requests_get_different_ids(client):
    first = client.get("/health").headers[REQUEST_ID_HEADER]
    second = client.get("/health").headers[REQUEST_ID_HEADER]

    assert first != second


# --- verbosity --------------------------------------------------------------


def test_health_probes_do_not_log_at_info(client, caplog):
    """Kubernetes probes every few seconds would otherwise be most of the log
    volume while saying nothing."""
    with caplog.at_level(logging.INFO):
        client.get("/health")

    assert [r for r in caplog.records if r.message == "request.handled"] == []


def test_real_requests_log_one_access_line_at_info(client, caplog):
    with caplog.at_level(logging.INFO):
        client.get("/documents", headers=auth())

    handled = [r for r in caplog.records if r.message == "request.handled"]
    assert len(handled) == 1
    assert handled[0].levelno == logging.INFO
    assert handled[0].status == 200
    assert handled[0].duration_ms >= 0


def test_a_rejected_upload_logs_at_warning_not_error(client, caplog):
    """A client sending a PNG is the API working, not the service failing.
    Paging on it is how alerting gets ignored."""
    with caplog.at_level(logging.INFO):
        client.post(
            "/documents",
            headers=auth(),
            files={"file": ("x.pdf", b"\x89PNG\r\n\x1a\n" + b"\x00" * 64, "image/png")},
        )

    rejected = [r for r in caplog.records if r.message == "request.rejected"]
    assert len(rejected) == 1
    assert rejected[0].levelno == logging.WARNING
    assert rejected[0].status == 415


def test_a_stored_upload_logs_the_business_event(client, caplog):
    with caplog.at_level(logging.INFO):
        client.post(
            "/documents",
            headers=auth(),
            files={"file": ("report.pdf", PDF, "application/pdf")},
        )

    stored = [r for r in caplog.records if r.message == "upload.stored"]
    assert len(stored) == 1
    assert stored[0].size_bytes == len(PDF)
    # A prefix is enough to correlate; the full digest is in the API response.
    assert len(stored[0].sha256_prefix) == 12


def test_an_unverified_webhook_logs_the_reason(client, caplog):
    with caplog.at_level(logging.INFO):
        client.post("/webhooks/partner", json={"job_id": "j_1"})

    unverified = [
        r for r in caplog.records if r.message == "webhook.partner.unverified"
    ]
    assert len(unverified) == 1
    assert unverified[0].levelno == logging.WARNING
    assert unverified[0].reason == "missing"


# --- the rendered output ----------------------------------------------------


@pytest.fixture
def rendered() -> Iterator[io.StringIO]:
    """Capture what is actually written, not just the LogRecords.

    contextvars are merged by the formatter at render time, so a test that only
    inspects records cannot see request_id or org_id at all -- exactly the
    fields most likely to break silently.
    """
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(build_formatter("json"))
    root = logging.getLogger()
    root.addHandler(handler)
    previous = root.level
    root.setLevel(logging.DEBUG)
    try:
        yield stream
    finally:
        root.removeHandler(handler)
        root.setLevel(previous)


def lines(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def test_the_tenant_and_request_are_attached_to_every_line_after_auth(client, rendered):
    """Correlation without threading org_id through a single call signature."""
    client.post(
        "/documents",
        headers=auth(),
        files={"file": ("report.pdf", PDF, "application/pdf")},
    )

    stored = next(line for line in lines(rendered) if line["event"] == "upload.stored")
    assert stored["org_id"] == str(ORG)
    assert stored["user_id"] == str(USER)
    assert stored["request_id"]
    assert stored["level"] == "info"


def test_one_request_id_ties_the_whole_request_together(client, rendered):
    response = client.post(
        "/documents",
        headers=auth(),
        files={"file": ("report.pdf", PDF, "application/pdf")},
    )

    emitted = lines(rendered)
    returned = response.headers[REQUEST_ID_HEADER]
    correlated = {
        line["event"] for line in emitted if line.get("request_id") == returned
    }

    # The id handed back to the caller is the one that finds the work.
    assert {"upload.stored", "request.handled"} <= correlated


def test_no_credential_ever_reaches_a_log_line(client, rendered):
    """An access token in a log file is a working credential, and logs get
    copied to places the database never goes."""
    access = token()

    client.post(
        "/documents",
        headers={"Authorization": f"Bearer {access}"},
        files={"file": ("report.pdf", PDF, "application/pdf")},
    )
    client.post(
        "/webhooks/partner",
        headers={"X-Partner-Signature": "deadbeef" * 8},
        json={"job_id": "j_1"},
    )

    emitted = rendered.getvalue()
    assert emitted, "nothing was captured; the fixture is not wired up"
    assert access not in emitted
    assert "deadbeef" not in emitted
    assert "password123" not in emitted
