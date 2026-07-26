"""OWASP security headers, applied to every response.

The header set comes from `secure`'s STRICT preset rather than being written
out here, so the values track that library's reading of the OWASP guidance
instead of drifting with ours.

Only the Content-Security-Policy is chosen locally, because CSP is the one
header whose correct value depends on what the response actually is. A JSON
body renders nothing and should permit nothing; the Swagger and ReDoc pages are
real documents that load assets from a CDN. The switch is keyed on the
response's Content-Type rather than on a list of paths, so a new HTML route
gets the document policy automatically and a new JSON route gets the strict one.
"""

from secure import ContentSecurityPolicy, Preset, Secure
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# FastAPI serves Swagger UI and ReDoc from jsdelivr with an inline bootstrap
# script; ReDoc additionally pulls Google fonts and spawns a blob: worker.
_CDN = "https://cdn.jsdelivr.net"
_FONTS = "https://fonts.gstatic.com"
_FONT_STYLES = "https://fonts.googleapis.com"
_FAVICON = "https://fastapi.tiangolo.com"

# Nothing renders in a JSON response, so nothing is allowed to load.
_API_CSP = ContentSecurityPolicy().default_src("'none'").frame_ancestors("'none'")

_DOCS_CSP = (
    ContentSecurityPolicy()
    .default_src("'self'")
    .base_uri("'self'")
    .frame_ancestors("'none'")
    .object_src("'none'")
    # 'unsafe-inline' is required by the inline bootstrap script FastAPI emits
    # into the docs page; a nonce would mean rewriting its HTML.
    .script_src("'self'", "'unsafe-inline'", _CDN)
    .style_src("'self'", "'unsafe-inline'", _CDN, _FONT_STYLES)
    .font_src("'self'", _CDN, _FONTS)
    .img_src("'self'", "data:", _FAVICON)
    .connect_src("'self'")
    .worker_src("'self'", "blob:")
)


# `Server` is dropped from every set: uvicorn appends its own banner at the
# protocol layer, after the ASGI app has returned, so setting it here yields two
# Server headers rather than replacing one. Suppressing the banner is the
# server's job -- see `--no-server-header` in the Dockerfile.
_NEVER = {"Server"}


def _headers_for(
    csp: ContentSecurityPolicy, *, drop: set[str] = frozenset()
) -> list[tuple[str, str]]:
    """Freeze one header set at import time; nothing is formatted per request."""
    merged = Secure.from_preset(Preset.STRICT).headers | {
        csp.header_name: csp.header_value
    }
    unwanted = _NEVER | drop
    return [(name, value) for name, value in merged.items() if name not in unwanted]


API_HEADERS = _headers_for(_API_CSP)
# Cross-Origin-Embedder-Policy: require-corp would block the CDN assets, which
# are not served with the CORP header that policy demands.
DOCS_HEADERS = _headers_for(_DOCS_CSP, drop={"Cross-Origin-Embedder-Policy"})


class SecurityHeadersMiddleware:
    """Attach the security header set to every response.

    Registered outermost so it also decorates responses produced by other
    middleware -- notably the 413 from MaxBodySizeMiddleware, which never
    reaches a route.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                document = headers.get("content-type", "").startswith("text/html")
                for name, value in DOCS_HEADERS if document else API_HEADERS:
                    # Assign rather than append: these replace whatever the app
                    # or the server already set, including uvicorn's `server`.
                    headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)
