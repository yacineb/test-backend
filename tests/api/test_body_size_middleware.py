"""The request-body guard, driven as raw ASGI.

Going through httpx would not let these tests observe *whether the body was
read*, which is the entire point of the Content-Length fast path.
"""

from collections.abc import Iterable

import pytest
from starlette.types import Receive, Scope, Send

from app.api.middleware import MaxBodySizeMiddleware

pytestmark = pytest.mark.anyio

MAX = 100


async def echo_length(scope: Scope, receive: Receive, send: Send) -> None:
    """Inner app that drains the whole body, like a form parser would."""
    size = 0
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        size += len(message.get("body", b""))
        if not message.get("more_body"):
            break
    body = str(size).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/plain")],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def call(
    headers: list[tuple[bytes, bytes]], chunks: Iterable[bytes]
) -> tuple[list[dict], list[bytes]]:
    app = MaxBodySizeMiddleware(echo_length, max_bytes=MAX)
    pending = list(chunks)
    consumed: list[bytes] = []
    sent: list[dict] = []

    async def receive() -> dict:
        if not pending:
            return {"type": "http.disconnect"}
        chunk = pending.pop(0)
        consumed.append(chunk)
        return {"type": "http.request", "body": chunk, "more_body": bool(pending)}

    async def send(message: dict) -> None:
        sent.append(message)

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    await app(scope, receive, send)
    return sent, consumed


def status_of(sent: list[dict]) -> int:
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


async def test_body_within_limit_passes_through():
    sent, consumed = await call([(b"content-length", b"10")], [b"x" * 10])

    assert status_of(sent) == 200
    assert consumed == [b"x" * 10]


async def test_oversized_content_length_is_rejected_without_reading_the_body():
    sent, consumed = await call(
        [(b"content-length", str(MAX + 1).encode())], [b"x" * (MAX + 1)]
    )

    assert status_of(sent) == 413
    # The claim being tested: not one byte of the body was pulled off the wire.
    assert consumed == []


async def test_exactly_at_limit_is_allowed():
    sent, _ = await call([(b"content-length", str(MAX).encode())], [b"x" * MAX])

    assert status_of(sent) == 200


async def test_chunked_body_exceeding_limit_is_aborted_mid_stream():
    # No content-length, as with Transfer-Encoding: chunked. The header fast
    # path cannot help here; the running counter is what stops it.
    chunks = [b"x" * 40] * 10  # 400 bytes total, limit is 100

    sent, consumed = await call([], chunks)

    assert status_of(sent) == 413
    # Aborted as soon as the count crossed, not after draining the client.
    assert len(consumed) == 3
    assert sum(len(c) for c in consumed) == 120


async def test_lying_content_length_does_not_defeat_the_counter():
    sent, _ = await call([(b"content-length", b"10")], [b"x" * 500])

    assert status_of(sent) == 413


async def test_non_http_scope_is_passed_through():
    called = False

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal called
        called = True

    app = MaxBodySizeMiddleware(inner, max_bytes=MAX)
    await app({"type": "lifespan"}, None, None)  # type: ignore[arg-type]

    assert called


@pytest.mark.parametrize("declared", [b"not-a-number", b"", b"-5"])
async def test_unparseable_content_length_falls_back_to_counting(declared: bytes):
    sent, _ = await call([(b"content-length", declared)], [b"x" * (MAX + 1)])

    assert status_of(sent) == 413
