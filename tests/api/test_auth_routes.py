"""Route-level tests with the persistence adapters swapped for fakes.

No database here: this covers wiring, status codes and the OpenAPI surface.
Isolation is proven for real in tests/integration.
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.api.deps import (
    get_auth_deps,
    get_organization_repository,
    get_token_service,
    get_user_repository,
)
from app.domain.organization import Organization
from app.main import app
from tests.fakes import (
    FakeOrganizationRepository,
    FakeUserRepository,
    make_deps,
    make_token_service,
    make_user,
)

PASSWORD = "password123"


@pytest.fixture
def user():
    return make_user(email="alice@acme.example.com", password=PASSWORD)


@pytest.fixture
def organization(user) -> Organization:
    return Organization(id=user.org_id, name="Acme Corp", slug="acme")


@pytest.fixture
def client(user, organization) -> Iterator[TestClient]:
    deps, _, _ = make_deps([user])

    async def override_auth_deps():
        yield deps

    app.dependency_overrides[get_auth_deps] = override_auth_deps
    app.dependency_overrides[get_token_service] = make_token_service
    app.dependency_overrides[get_user_repository] = lambda: FakeUserRepository([user])
    app.dependency_overrides[get_organization_repository] = lambda: (
        FakeOrganizationRepository([organization])
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


def login(client: TestClient, password: str = PASSWORD) -> dict:
    response = client.post(
        "/auth/login", json={"email": "alice@acme.example.com", "password": password}
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_health_still_works(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_login_returns_a_bearer_pair(client):
    body = login(client)

    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 6 * 3600
    assert body["access_token"] and body["refresh_token"]


def test_login_with_wrong_password_is_401(client):
    response = client.post(
        "/auth/login", json={"email": "alice@acme.example.com", "password": "wrong"}
    )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_login_with_malformed_email_is_422(client):
    response = client.post(
        "/auth/login", json={"email": "not-an-email", "password": PASSWORD}
    )

    assert response.status_code == 422


def test_me_exposes_user_id_and_org_id(client, user, organization):
    token = login(client)["access_token"]

    response = client.get("/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    body = response.json()
    assert body["user_id"] == str(user.id)
    assert body["org_id"] == str(organization.id)
    assert body["org_slug"] == "acme"


def test_me_without_a_token_is_401(client):
    response = client.get("/me")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_me_with_a_garbage_token_is_401(client):
    response = client.get("/me", headers={"Authorization": "Bearer nonsense"})

    assert response.status_code == 401


def test_me_rejects_a_refresh_token_used_as_a_bearer(client):
    """The refresh token is opaque, so it must not authenticate anything."""
    refresh_token = login(client)["refresh_token"]

    response = client.get("/me", headers={"Authorization": f"Bearer {refresh_token}"})

    assert response.status_code == 401


def test_refresh_rotates_and_the_old_token_stops_working(client):
    first = login(client)["refresh_token"]

    rotated = client.post("/auth/refresh", json={"refresh_token": first})
    assert rotated.status_code == 200
    assert rotated.json()["refresh_token"] != first

    replayed = client.post("/auth/refresh", json={"refresh_token": first})
    assert replayed.status_code == 401


def test_refresh_with_unknown_token_is_401(client):
    response = client.post("/auth/refresh", json={"refresh_token": "nope"})

    assert response.status_code == 401


def test_logout_is_204_and_kills_the_refresh_token(client):
    refresh_token = login(client)["refresh_token"]

    assert (
        client.post("/auth/logout", json={"refresh_token": refresh_token}).status_code
        == 204
    )

    replayed = client.post("/auth/refresh", json={"refresh_token": refresh_token})
    assert replayed.status_code == 401


def test_logout_of_an_unknown_token_is_still_204(client):
    response = client.post("/auth/logout", json={"refresh_token": "nope"})

    assert response.status_code == 204


def test_openapi_advertises_the_bearer_scheme(client):
    """Swagger needs this to render the Authorize button."""
    schema = client.get("/openapi.json").json()

    assert "Bearer" in schema["components"]["securitySchemes"]
    assert schema["paths"]["/me"]["get"]["security"]


def test_disabled_account_is_403(client, user):
    from dataclasses import replace

    deps, _, _ = make_deps([replace(user, is_active=False)])

    async def override():
        yield deps

    app.dependency_overrides[get_auth_deps] = override

    response = client.post(
        "/auth/login", json={"email": "alice@acme.example.com", "password": PASSWORD}
    )

    assert response.status_code == 403
