"""OWASP header coverage, and proof the docs UI is not broken by its own CSP."""

import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.middleware import MaxBodySizeMiddleware
from app.api.security_headers import SecurityHeadersMiddleware
from app.main import app

client = TestClient(app)

# Headers every response must carry, whatever it is.
REQUIRED = [
    "content-security-policy",
    "strict-transport-security",
    "x-content-type-options",
    "x-frame-options",
    "referrer-policy",
    "cross-origin-opener-policy",
    "permissions-policy",
]


def directives(csp: str) -> dict[str, str]:
    out = {}
    for part in csp.split(";"):
        part = part.strip()
        if part:
            name, _, value = part.partition(" ")
            out[name] = value
    return out


@pytest.mark.parametrize("header", REQUIRED)
def test_json_responses_carry_the_owasp_headers(header):
    response = client.get("/health")

    assert response.status_code == 200
    assert header in response.headers


def test_json_responses_forbid_loading_anything():
    csp = directives(client.get("/health").headers["content-security-policy"])

    # A JSON body renders nothing, so nothing should be fetchable from it.
    assert csp["default-src"] == "'none'"
    assert csp["frame-ancestors"] == "'none'"


def test_middleware_does_not_emit_a_server_header():
    """Suppressing the banner belongs to uvicorn (`--no-server-header`).

    Setting it here instead produces *two* Server headers in production, because
    uvicorn appends its own after the ASGI app has returned — a duplicate that
    TestClient cannot reveal, since it never adds the banner in the first place.
    """
    assert "server" not in client.get("/health").headers


def test_docs_page_is_served_as_a_document_with_its_own_policy():
    response = client.get("/docs")

    assert response.status_code == 200
    csp = directives(response.headers["content-security-policy"])
    assert csp["default-src"] == "'self'"
    # Still not embeddable, and still cannot load plugins.
    assert csp["frame-ancestors"] == "'none'"
    assert csp["object-src"] == "'none'"


@pytest.mark.parametrize("page", ["/docs", "/redoc"])
def test_every_external_asset_the_docs_page_loads_is_permitted_by_its_csp(page):
    """The failure this guards against: shipping a CSP that blanks out /docs.

    The exercise requires Swagger to be usable end to end, and a strict CSP is
    exactly the kind of change that breaks it silently — the page returns 200
    and renders nothing.
    """
    response = client.get(page)
    html = response.text
    csp = directives(response.headers["content-security-policy"])

    loads: list[tuple[str, str]] = [
        ("script-src", url)
        for url in re.findall(r'<script[^>]+src="(https://[^"]+)"', html)
    ]
    loads += [
        ("img-src", url) for url in re.findall(r'<img[^>]+src="(https://[^"]+)"', html)
    ]
    # <link> is governed by the directive for what it pulls in, not by the tag:
    # a stylesheet is style-src, a favicon is img-src.
    for tag in re.findall(r"<link[^>]+>", html):
        url_match = re.search(r'href="(https://[^"]+)"', tag)
        if not url_match:
            continue
        rel_match = re.search(r'rel="([^"]+)"', tag)
        rel = rel_match.group(1) if rel_match else ""
        loads.append(("img-src" if "icon" in rel else "style-src", url_match.group(1)))

    assert loads, f"{page} loaded no external assets; the regexes are stale"

    for directive, url in loads:
        origin = "/".join(url.split("/")[:3])
        assert origin in csp[directive], (
            f"{page} loads {url} but {directive} does not allow {origin}"
        )


def test_docs_does_not_send_an_embedder_policy_that_would_block_the_cdn():
    # require-corp would reject the jsdelivr assets, which carry no CORP header.
    assert "cross-origin-embedder-policy" not in client.get("/docs").headers


def test_headers_reach_responses_that_never_touch_a_route():
    """Ordering check: the body guard answers before routing, and must still
    come back wrapped in the security headers."""
    guarded = FastAPI()
    guarded.add_middleware(MaxBodySizeMiddleware, max_bytes=10)
    guarded.add_middleware(SecurityHeadersMiddleware)

    @guarded.post("/echo")
    def echo() -> dict:  # pragma: no cover - the body never gets this far
        return {}

    response = TestClient(guarded).post("/echo", content=b"x" * 50)

    assert response.status_code == 413
    for header in REQUIRED:
        assert header in response.headers
