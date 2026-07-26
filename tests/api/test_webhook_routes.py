"""Route-level tests for the inbound partner webhook.

The sink is the in-memory stub, which is what runs on this branch anyway; what
is under test is the signature gate, the status codes, and the fact that the
signing helper produces something the webhook actually accepts.
"""

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.api.deps import SIGNATURE_HEADER, get_settings, get_webhook_deps
from app.application.deps import WebhookDeps
from app.infrastructure.clock import SystemClock
from app.infrastructure.partner_jobs import InMemoryPartnerJobSink
from app.infrastructure.security.signatures import HmacSha256Signer
from app.main import app
from tests.fakes import RejectingPartnerJobSink

SECRET = "partner-secret-at-least-32-bytes-ok!"
TOLERANCE = timedelta(minutes=5)
JOB_ID = "j_abc123def4567890"

JSON_HEADERS = {"Content-Type": "application/json"}


def sign(body: bytes) -> dict[str, str]:
    """Signs the way the partner does. That HmacSha256Signer computes the right
    thing is pinned independently in tests/unit/test_webhook_signature.py."""
    return JSON_HEADERS | {SIGNATURE_HEADER: HmacSha256Signer(SECRET).sign(body)}


def payload(**overrides) -> bytes:
    body = {
        "job_id": JOB_ID,
        "status": "completed",
        "result": {"indexed_at": "2026-05-21T14:23:11Z"},
        "occurred_at": datetime.now(UTC).isoformat(),
    }
    body.update(overrides)
    return json.dumps(body).encode()


@pytest.fixture
def sink() -> InMemoryPartnerJobSink:
    return InMemoryPartnerJobSink()


@pytest.fixture
def client(sink, monkeypatch) -> Iterator[TestClient]:
    # The secret goes in through the environment rather than a Settings
    # override: nested settings sections re-read the environment themselves, so
    # PARTNER_HMAC_SECRET is the only path that actually reaches the signer —
    # and it is the one operators use.
    monkeypatch.setenv("PARTNER_HMAC_SECRET", SECRET)
    monkeypatch.setenv(
        "PARTNER_WEBHOOK_TOLERANCE_SECONDS", str(int(TOLERANCE.total_seconds()))
    )
    get_settings.cache_clear()

    app.dependency_overrides[get_webhook_deps] = lambda: WebhookDeps(
        sink=sink, clock=SystemClock(), tolerance=TOLERANCE
    )
    yield TestClient(app)
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def test_a_correctly_signed_notification_is_accepted(client, sink):
    body = payload()

    response = client.post("/webhooks/partner", content=body, headers=sign(body))

    assert response.status_code == 202, response.text
    assert response.json() == {"job_id": JOB_ID}
    assert [n.job_id for n in sink.received] == [JOB_ID]
    assert sink.received[0].result == {"indexed_at": "2026-05-21T14:23:11Z"}


def test_a_failed_status_is_accepted_too(client, sink):
    body = payload(status="failed", result=None)

    response = client.post("/webhooks/partner", content=body, headers=sign(body))

    assert response.status_code == 202
    assert sink.received[0].status == "failed"


def test_without_the_signature_header_it_is_401(client, sink):
    response = client.post("/webhooks/partner", content=payload(), headers=JSON_HEADERS)

    assert response.status_code == 401
    assert not sink.received


def test_a_wrong_signature_is_401(client, sink):
    headers = JSON_HEADERS | {SIGNATURE_HEADER: "00" * 32}

    response = client.post("/webhooks/partner", content=payload(), headers=headers)

    assert response.status_code == 401
    assert not sink.received


def test_a_signature_over_different_bytes_is_401(client, sink):
    """Whitespace counts: the signature covers what was sent, not what it means."""
    headers = sign(payload())

    response = client.post(
        "/webhooks/partner", content=payload() + b" ", headers=headers
    )

    assert response.status_code == 401
    assert not sink.received


def test_the_401_carries_no_bearer_challenge(client):
    """This route is not part of the JWT surface; offering a challenge lies."""
    response = client.post("/webhooks/partner", content=payload(), headers=JSON_HEADERS)

    assert response.status_code == 401
    assert "www-authenticate" not in response.headers


def test_unsigned_and_malformed_is_401_not_422(client, sink):
    """Proof the gate runs before the parser: an unsigned body is never parsed."""
    body = b'{"garbage": true}'

    response = client.post("/webhooks/partner", content=body, headers=JSON_HEADERS)

    assert response.status_code == 401
    assert not sink.received


def test_signed_but_malformed_is_422(client, sink):
    body = b'{"garbage": true}'

    response = client.post("/webhooks/partner", content=body, headers=sign(body))

    assert response.status_code == 422
    assert not sink.received


def test_an_unknown_status_is_422(client, sink):
    body = payload(status="half-done")

    response = client.post("/webhooks/partner", content=body, headers=sign(body))

    assert response.status_code == 422
    assert not sink.received


def test_a_naive_occurred_at_is_422_not_a_crash(client, sink):
    """A timestamp with no timezone cannot be compared to now. Reject, not guess."""
    body = payload(occurred_at=datetime.now(UTC).replace(tzinfo=None).isoformat())

    response = client.post("/webhooks/partner", content=body, headers=sign(body))

    assert response.status_code == 422
    assert not sink.received


def test_a_stale_notification_is_400(client, sink):
    body = payload(occurred_at=(datetime.now(UTC) - timedelta(hours=1)).isoformat())

    response = client.post("/webhooks/partner", content=body, headers=sign(body))

    assert response.status_code == 400
    assert not sink.received


def test_an_unknown_job_id_is_404(client):
    """Wired now so the pipeline branch only has to raise it."""
    app.dependency_overrides[get_webhook_deps] = lambda: WebhookDeps(
        sink=RejectingPartnerJobSink(), clock=SystemClock(), tolerance=TOLERANCE
    )
    body = payload()

    response = client.post("/webhooks/partner", content=body, headers=sign(body))

    assert response.status_code == 404


def test_the_signing_helper_produces_a_body_the_webhook_accepts(client, sink):
    """The end-to-end path a human takes in /docs, in one test."""
    body = payload()

    helper = client.post("/webhooks/partner/sign", content=body, headers=JSON_HEADERS)
    assert helper.status_code == 200, helper.text
    signed = helper.json()

    response = client.post(
        "/webhooks/partner",
        content=body,
        headers=JSON_HEADERS | {SIGNATURE_HEADER: signed["signature"]},
    )

    assert response.status_code == 202
    assert [n.job_id for n in sink.received] == [JOB_ID]


def test_the_signing_helper_echoes_the_bytes_it_signed(client):
    """Whitespace and key order included, so the echo is safe to send back."""
    body = (
        b'{  "status" : "completed" ,\n'
        b' "job_id":"j_1", "occurred_at":"2026-05-21T14:23:11Z"}'
    )

    signed = client.post(
        "/webhooks/partner/sign", content=body, headers=JSON_HEADERS
    ).json()

    assert signed["body"].encode() == body


def test_the_signing_helper_refuses_a_body_the_webhook_would_reject(client):
    """Signing garbage would just move the 422 one request later."""
    response = client.post(
        "/webhooks/partner/sign", content=b'{"garbage": true}', headers=JSON_HEADERS
    )

    assert response.status_code == 422


def test_the_signing_helper_can_be_turned_off(client, monkeypatch):
    monkeypatch.setenv("PARTNER_WEBHOOK_SIGNING_HELPER", "false")
    get_settings.cache_clear()

    response = client.post(
        "/webhooks/partner/sign", content=payload(), headers=JSON_HEADERS
    )

    assert response.status_code == 404


def test_the_signing_helper_is_documented_with_an_editable_body(client):
    """Without a requestBody in the schema, /docs renders no textarea to type in."""
    schema = client.get("/openapi.json").json()
    request_body = schema["paths"]["/webhooks/partner/sign"]["post"]["requestBody"]
    ref = request_body["content"]["application/json"]["schema"]["$ref"]

    assert ref == "#/components/schemas/PartnerWebhookRequest"
    assert (
        "job_id"
        in schema["components"]["schemas"]["PartnerWebhookRequest"]["properties"]
    )


def test_the_webhook_is_not_behind_the_bearer_scheme(client):
    """The partner has no JWT. If this route ever grows `security`, it breaks."""
    schema = client.get("/openapi.json").json()

    assert "security" not in schema["paths"]["/webhooks/partner"]["post"]
