import json

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class _BodyTooLarge(Exception):
    """Internal signal, raised out of `receive` and caught in this module."""


class MaxBodySizeMiddleware:
    """Reject request bodies larger than `max_bytes`, before anything parses them.

    This cannot live in the endpoint. Starlette's multipart parser writes file
    parts into a SpooledTemporaryFile as it reads, and its `max_part_size`
    applies only to non-file parts -- so by the time handler code runs, an
    unbounded amount of client data has already been written to the server's
    temp directory. This middleware runs ahead of the parser and is the only
    thing standing between a hostile client and the disk.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = Headers(scope=scope).get("content-length")
        if (
            declared is not None
            and declared.isdigit()
            and int(declared) > self.max_bytes
        ):
            await self._reject(send)
            return

        # Content-Length is absent under chunked encoding and is client-supplied
        # either way, so the header check above is an optimisation, not the
        # enforcement. This is the enforcement.
        received = 0
        response_started = False

        async def counting_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise _BodyTooLarge
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except _BodyTooLarge:
            if not response_started:
                await self._reject(send)

    async def _reject(self, send: Send) -> None:
        body = json.dumps(
            {"detail": f"request body exceeds {self.max_bytes} bytes"}
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
