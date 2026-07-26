"""Request correlation and the access log.

This is the tracing backbone: every request gets an id, that id is bound for the
life of the request so every log line downstream carries it, and it goes back to
the caller in `X-Request-Id`. A user reporting a failure can quote one string
that finds the upload, the four pipeline steps and the webhook that finished it.

An inbound `X-Request-Id` is honoured so a caller or gateway can correlate
across services, but it is sanitised first -- it is attacker-controlled and ends
up in log output, where an unbounded or newline-bearing value is log injection.
"""

import logging
import re
import time
import uuid

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.observability import bind_context, clear_context

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "x-request-id"

# Conservative on purpose: this value is echoed into every log line for the
# request, so anything that could forge a field or break a parser is refused
# and replaced rather than escaped.
_SAFE_REQUEST_ID = re.compile(r"\A[A-Za-z0-9._\-]{1,64}\Z")

# Probes run every few seconds forever. At INFO they would be the majority of
# the log volume and would say nothing.
_QUIET_PATHS = frozenset({"/health"})


def _incoming_request_id(headers: Headers) -> str:
    supplied = headers.get(REQUEST_ID_HEADER)
    if supplied and _SAFE_REQUEST_ID.match(supplied):
        return supplied
    return uuid.uuid4().hex


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _incoming_request_id(Headers(scope=scope))
        method = scope.get("method", "")
        path = scope.get("path", "")

        clear_context()
        bind_context(request_id=request_id, method=method, path=path)

        status = 500
        started = time.perf_counter()

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        except Exception:
            # Logged here because this is the last place that still knows the
            # request; the exception itself continues to the error handlers.
            logger.exception(
                "request.failed",
                extra={"duration_ms": round((time.perf_counter() - started) * 1000, 2)},
            )
            raise
        else:
            level = logging.DEBUG if path in _QUIET_PATHS else logging.INFO
            # 5xx means we broke it, and it deserves attention even though the
            # response was produced without raising.
            if status >= 500:
                level = logging.ERROR
            logger.log(
                level,
                "request.handled",
                extra={
                    "status": status,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )
        finally:
            clear_context()
