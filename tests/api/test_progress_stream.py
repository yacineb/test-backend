"""The SSE endpoint, with persistence and the hub swapped for fakes.

What is under test here is the auth gate, the content type, and the promise
that the first thing on the wire is a full snapshot. That the database actually
notifies is proven in tests/integration/test_progress_notify.py.
"""

import asyncio
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.deps import (
    get_document_repository,
    get_document_transaction,
    get_progress_hub,
    get_token_service,
)
from app.domain.auth import AuthContext
from app.domain.document import DocumentStatus
from app.infrastructure.progress import ProgressHub
from app.main import app
from tests.fakes import (
    FakeDocumentTransaction,
    make_document,
    make_token_service,
)

ORG = uuid4()
OTHER_ORG = uuid4()
USER = uuid4()


@pytest.fixture
def wiring():
    transaction = FakeDocumentTransaction()
    transaction.repository.org_id = ORG
    hub = ProgressHub()
    return transaction, hub


@pytest.fixture
def client(wiring) -> Iterator[TestClient]:
    transaction, hub = wiring
    app.dependency_overrides[get_token_service] = make_token_service
    app.dependency_overrides[get_document_repository] = lambda: transaction.repository
    app.dependency_overrides[get_document_transaction] = lambda: transaction
    app.dependency_overrides[get_progress_hub] = lambda: hub
    yield TestClient(app)
    app.dependency_overrides.clear()


def token_for(org_id=ORG) -> str:
    access, _ = make_token_service().issue_access_token(
        AuthContext(user_id=USER, org_id=org_id), datetime.now(UTC)
    )
    return access


def auth(org_id=ORG) -> dict[str, str]:
    return {"Authorization": f"Bearer {token_for(org_id)}"}


def seed(transaction, status=DocumentStatus.READY):
    """A terminal document, so the stream sends one snapshot and closes.

    asyncio.run is fine here: the fake does no I/O, and TestClient is sync.
    """
    document = make_document(ORG, status=status)
    asyncio.run(transaction.repository.add(document))
    return document


def test_streaming_requires_a_token(client):
    assert client.get(f"/documents/{uuid4()}/events").status_code == 401


def test_another_orgs_document_is_404(client, wiring):
    transaction, _ = wiring

    response = client.get(f"/documents/{uuid4()}/events", headers=auth(OTHER_ORG))

    assert response.status_code == 404


def test_a_terminal_document_yields_one_snapshot_and_closes(client, wiring):
    transaction, _ = wiring
    document = seed(transaction)

    with client.stream(
        "GET", f"/documents/{document.id}/events", headers=auth()
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        # Set by SecurityHeadersMiddleware, not by the endpoint, and stronger
        # than the no-cache an SSE route would set for itself.
        assert response.headers["cache-control"] == "no-store, max-age=0"
        assert response.headers["x-accel-buffering"] == "no"
        body = "".join(response.iter_text())

    # Snapshot first, always - which is what makes a reconnect need no replay.
    assert body.startswith("event: progress\nid: 1\ndata: ")
    assert '"status":"ready"' in body
    # Terminal, so the stream ended rather than idling on a heartbeat.
    assert ": ping" not in body


def test_the_snapshot_carries_the_same_body_as_the_detail_endpoint(client, wiring):
    """One schema for both, so a client can poll or stream interchangeably."""
    transaction, _ = wiring
    document = seed(transaction)

    with client.stream(
        "GET", f"/documents/{document.id}/events", headers=auth()
    ) as response:
        streamed = "".join(response.iter_text())
    polled = client.get(f"/documents/{document.id}", headers=auth()).json()

    payload = streamed.split("data: ", 1)[1].split("\n\n", 1)[0]
    assert json.loads(payload) == polled
