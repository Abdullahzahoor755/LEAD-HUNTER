from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import pytest

import app.api.app as api_module
from app.api.app import create_fastapi_app
from app.core.auth import decode_jwt_token
from app.core.models import Tenant, TenantContext
from app.db.session import build_memory_session
from app.services.auth_service import AuthService


def _configure_google_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.configs.settings.settings.google_auth_client_id", "google-auth-client-id")
    monkeypatch.setattr("app.configs.settings.settings.google_auth_client_secret", "google-auth-client-secret")
    monkeypatch.setattr("app.configs.settings.settings.google_auth_redirect_uri", "http://testserver/auth/google/callback")
    monkeypatch.setattr("app.configs.settings.settings.frontend_base_url", "http://frontend.test")


async def _google_state(client: httpx.AsyncClient) -> str:
    response = await client.get("/auth/google/start")
    assert response.status_code == 200
    authorization_url = response.json()["authorization_url"]
    return parse_qs(urlparse(authorization_url).query)["state"][0]


async def _mock_google_profile(
    monkeypatch: pytest.MonkeyPatch,
    *,
    email: str,
    verified: bool = True,
    name: str = "Google User",
    sub: str = "google-sub-123",
) -> None:
    async def fake_exchange(code: str, redirect_uri: str):
        assert code == "code-123"
        assert redirect_uri == "http://testserver/auth/google/callback"
        return {"access_token": "google-access-token"}

    async def fake_profile(access_token: str):
        assert access_token == "google-access-token"
        return {
            "sub": sub,
            "email": email,
            "email_verified": verified,
            "name": name,
            "picture": "https://example.test/avatar.png",
        }

    monkeypatch.setattr(api_module, "exchange_google_auth_code", fake_exchange)
    monkeypatch.setattr(api_module, "fetch_google_auth_profile", fake_profile)


@pytest.mark.anyio
async def test_google_start_returns_auth_url_without_gmail_scopes(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_google_auth(monkeypatch)
    app = create_fastapi_app(db=build_memory_session())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/auth/google/start")

    assert response.status_code == 200
    payload = response.json()
    authorization_url = payload["authorization_url"]
    parsed = urlparse(authorization_url)
    query = parse_qs(parsed.query)
    scopes = query["scope"][0].split()
    assert parsed.netloc == "accounts.google.com"
    assert query["client_id"] == ["google-auth-client-id"]
    assert query["redirect_uri"] == ["http://testserver/auth/google/callback"]
    assert scopes == ["openid", "email", "profile"]
    assert "gmail" not in query["scope"][0].lower()
    assert "google-auth-client-secret" not in authorization_url
    assert query["state"][0]


@pytest.mark.anyio
async def test_google_callback_creates_new_free_user(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_google_auth(monkeypatch)
    await _mock_google_profile(monkeypatch, email="john.doe@example.com", name="John Doe")
    db = build_memory_session()
    app = create_fastapi_app(db=db)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        state = await _google_state(client)
        response = await client.get(f"/auth/google/callback?code=code-123&state={state}", follow_redirects=False)

    assert response.status_code == 302
    location = response.headers["location"]
    query = parse_qs(urlparse(location).query)
    token = query["auth_token"][0]
    payload = decode_jwt_token(token)
    assert payload["tenant_id"] == "john-doe"
    assert payload["role"] == "member"
    assert payload["plan"] == "Free"
    tenant = db.tenants.list("john-doe")[0]
    user = db.users.find_by_email("john-doe", "john.doe@example.com")
    assert tenant.subscription_plan == "Free"
    assert tenant.slug == "john-doe"
    assert user is not None
    assert user.role == "member"
    assert user.status == "active"


@pytest.mark.anyio
async def test_google_callback_appends_slug_suffix_for_new_user_collision(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_google_auth(monkeypatch)
    await _mock_google_profile(monkeypatch, email="john.doe@example.com", name="John Doe")
    db = build_memory_session()
    db.tenants.save(Tenant(tenant_id="existing-tenant", name="Existing", slug="john-doe", subscription_plan="Pro"))
    app = create_fastapi_app(db=db)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        state = await _google_state(client)
        response = await client.get(f"/auth/google/callback?code=code-123&state={state}", follow_redirects=False)

    payload = decode_jwt_token(parse_qs(urlparse(response.headers["location"]).query)["auth_token"][0])
    assert response.status_code == 302
    assert payload["tenant_id"] == "john-doe-1"
    assert db.tenants.list("john-doe-1")[0].slug == "john-doe-1"
    assert db.tenants.list("existing-tenant")[0].slug == "john-doe"


@pytest.mark.anyio
async def test_google_callback_logs_in_existing_user(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_google_auth(monkeypatch)
    await _mock_google_profile(monkeypatch, email="owner@example.com", name="Owner")
    db = build_memory_session()
    existing = await AuthService(db).signup(
        tenant_id="tenant-existing-google",
        tenant_name="Existing",
        tenant_slug="tenant-existing-google",
        email="owner@example.com",
        password="secret123",
        full_name="Owner",
        plan="Pro",
    )
    app = create_fastapi_app(db=db)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        state = await _google_state(client)
        response = await client.get(f"/auth/google/callback?code=code-123&state={state}", follow_redirects=False)

    payload = decode_jwt_token(parse_qs(urlparse(response.headers["location"]).query)["auth_token"][0])
    assert response.status_code == 302
    assert payload["tenant_id"] == existing.tenant_id
    assert payload["email"] == "owner@example.com"
    assert payload["plan"] == "Pro"
    assert payload["role"] == "owner"


@pytest.mark.anyio
async def test_google_callback_existing_admin_keeps_admin_role(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_google_auth(monkeypatch)
    await _mock_google_profile(monkeypatch, email="admin@example.com", name="Admin")
    db = build_memory_session()
    await AuthService(db).signup(
        tenant_id="tenant-google-admin",
        tenant_name="Admin",
        tenant_slug="tenant-google-admin",
        email="admin@example.com",
        password="secret123",
        full_name="Admin",
        plan="Agency",
        role="admin",
    )
    app = create_fastapi_app(db=db)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        state = await _google_state(client)
        response = await client.get(f"/auth/google/callback?code=code-123&state={state}", follow_redirects=False)

    payload = decode_jwt_token(parse_qs(urlparse(response.headers["location"]).query)["auth_token"][0])
    user = db.users.find_by_email("tenant-google-admin", "admin@example.com")
    assert response.status_code == 302
    assert payload["role"] == "admin"
    assert user is not None
    assert user.role == "admin"


@pytest.mark.anyio
async def test_google_callback_invalid_state_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_google_auth(monkeypatch)
    app = create_fastapi_app(db=build_memory_session())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/auth/google/callback?code=code-123&state=bad-state", follow_redirects=False)

    assert response.status_code == 302
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query["google_auth"] == ["error"]
    assert "state" in query["message"][0].lower()


@pytest.mark.anyio
async def test_google_callback_rejects_unverified_email(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_google_auth(monkeypatch)
    await _mock_google_profile(monkeypatch, email="new@example.com", verified=False)
    app = create_fastapi_app(db=build_memory_session())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        state = await _google_state(client)
        response = await client.get(f"/auth/google/callback?code=code-123&state={state}", follow_redirects=False)

    query = parse_qs(urlparse(response.headers["location"]).query)
    assert response.status_code == 302
    assert query["google_auth"] == ["error"]
    assert "verified" in query["message"][0].lower()


@pytest.mark.anyio
async def test_existing_password_login_still_works_with_google_auth_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_google_auth(monkeypatch)
    db = build_memory_session()
    await AuthService(db).signup(
        tenant_id="tenant-password-still-works",
        tenant_name="Password Tenant",
        tenant_slug="tenant-password-still-works",
        email="owner@password.test",
        password="secret123",
        full_name="Owner",
        plan="Free",
    )
    app = create_fastapi_app(db=db)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/login",
            json={"tenant_id": "tenant-password-still-works", "email": "owner@password.test", "password": "secret123"},
        )

    assert response.status_code == 200
    assert response.json()["token"]
    assert response.json()["email"] == "owner@password.test"
