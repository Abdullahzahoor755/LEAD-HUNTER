from __future__ import annotations

from urllib.parse import parse_qs, urlparse
from types import SimpleNamespace

import httpx
import pytest

from app.agents.base import AgentRequest
from app.agents.outreach import OutreachAgent
from app.api.app import create_fastapi_app
from app.core.models import Lead, TenantContext
from app.db.session import build_memory_session


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _signup(client: httpx.AsyncClient, tenant_id: str = "tenant-gmail", plan: str = "Pro") -> dict[str, str]:
    response = await client.post(
        "/signup",
        json={
            "tenant_id": tenant_id,
            "tenant_name": "Tenant Gmail",
            "tenant_slug": tenant_id,
            "email": f"owner@{tenant_id}.test",
            "password": "secret123",
            "full_name": "Tenant Owner",
            "plan": plan,
        },
    )
    assert response.status_code == 200
    return response.json()


def _configure_google_oauth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.configs.settings.settings.secret_encryption_key", "test-key")
    monkeypatch.setattr("app.configs.settings.settings.google_oauth_client_id", "google-client-id")
    monkeypatch.setattr("app.configs.settings.settings.google_oauth_client_secret", "google-client-secret")
    monkeypatch.setattr(
        "app.configs.settings.settings.google_oauth_redirect_uri",
        "http://testserver/settings/providers/gmail/oauth/callback",
    )
    monkeypatch.setattr("app.configs.settings.settings.frontend_base_url", "http://frontend.test")


def _clear_google_oauth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "")
    monkeypatch.setenv("GOOGLE_OAUTH_REDIRECT_URI", "")
    monkeypatch.setattr("app.configs.settings.settings.google_oauth_client_id", "")
    monkeypatch.setattr("app.configs.settings.settings.google_oauth_client_secret", "")
    monkeypatch.setattr("app.configs.settings.settings.google_oauth_redirect_uri", "")


async def _oauth_state(client: httpx.AsyncClient, token: str) -> str:
    response = await client.get("/settings/providers/gmail/oauth/start", headers=_auth_headers(token))
    assert response.status_code == 200
    authorization_url = response.json()["authorization_url"]
    return parse_qs(urlparse(authorization_url).query)["state"][0]


@pytest.mark.anyio
async def test_gmail_oauth_start_missing_env_returns_safe_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_google_oauth(monkeypatch)
    app = create_fastapi_app(db=build_memory_session())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        signup = await _signup(client, tenant_id="tenant-gmail-missing-env")
        response = await client.get("/settings/providers/gmail/oauth/start", headers=_auth_headers(signup["token"]))

    assert response.status_code == 400
    payload = response.json()
    assert payload == {"detail": "Google OAuth is not configured."}
    assert "GOOGLE_OAUTH_CLIENT_SECRET" not in str(payload)
    assert "google-client-secret" not in str(payload)


@pytest.mark.anyio
async def test_gmail_oauth_start_missing_settings_attrs_returns_safe_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_REDIRECT_URI", raising=False)
    monkeypatch.setattr(
        "app.api.app.settings",
        SimpleNamespace(frontend_base_url="", environment="development", database_backend="postgres"),
    )
    app = create_fastapi_app(db=build_memory_session())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        signup = await _signup(client, tenant_id="tenant-gmail-missing-attrs")
        response = await client.get("/settings/providers/gmail/oauth/start", headers=_auth_headers(signup["token"]))

    assert response.status_code == 400
    assert response.json() == {"detail": "Google OAuth is not configured."}


@pytest.mark.anyio
async def test_gmail_oauth_start_returns_authorization_url(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_google_oauth(monkeypatch)
    app = create_fastapi_app(db=build_memory_session())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        signup = await _signup(client)
        response = await client.get("/settings/providers/gmail/oauth/start", headers=_auth_headers(signup["token"]))

    assert response.status_code == 200
    payload = response.json()
    authorization_url = payload["authorization_url"]
    parsed = urlparse(authorization_url)
    query = parse_qs(parsed.query)
    assert parsed.netloc == "accounts.google.com"
    assert query["client_id"] == ["google-client-id"]
    assert query["access_type"] == ["offline"]
    assert "https://www.googleapis.com/auth/gmail.send" in query["scope"][0]
    assert "https://www.googleapis.com/auth/gmail.readonly" in query["scope"][0]
    assert query["state"][0]
    assert "google-client-secret" not in authorization_url
    assert "GOOGLE_OAUTH_CLIENT_SECRET" not in str(payload)


@pytest.mark.anyio
async def test_gmail_oauth_callback_rejects_invalid_state(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_google_oauth(monkeypatch)
    app = create_fastapi_app(db=build_memory_session())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(
            "/settings/providers/gmail/oauth/callback",
            params={"code": "auth-code", "state": "not-a-valid-state"},
        )

    assert response.status_code == 302
    assert "gmail_oauth=error" in response.headers["location"]


@pytest.mark.anyio
async def test_gmail_oauth_callback_stores_encrypted_refresh_token_and_sender(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_google_oauth(monkeypatch)

    async def fake_exchange(code: str, redirect_uri: str) -> dict[str, object]:
        assert code == "auth-code"
        assert redirect_uri == "http://testserver/settings/providers/gmail/oauth/callback"
        return {"refresh_token": "refresh-token", "access_token": "access-token", "expires_in": 3600}

    async def fake_profile(access_token: str) -> str:
        assert access_token == "access-token"
        return "sender@gmail.com"

    monkeypatch.setattr("app.api.app.exchange_gmail_oauth_code", fake_exchange)
    monkeypatch.setattr("app.api.app.fetch_gmail_profile_email", fake_profile)
    db = build_memory_session()
    app = create_fastapi_app(db=db)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        signup = await _signup(client, tenant_id="tenant-oauth-callback")
        state = await _oauth_state(client, signup["token"])
        response = await client.get(
            "/settings/providers/gmail/oauth/callback",
            params={"code": "auth-code", "state": state},
        )

    assert response.status_code == 302
    assert "gmail_oauth=success" in response.headers["location"]
    tenant = db.tenants.list("tenant-oauth-callback")[0]
    gmail = tenant.settings["providers"]["gmail"]
    assert gmail["client_id"] == "google-client-id"
    assert gmail["email_address"] == "sender@gmail.com"
    assert gmail["connected"] is True
    assert gmail["refresh_token_encrypted"] != "refresh-token"
    assert "refresh_token" not in gmail
    assert "client_secret" not in gmail


@pytest.mark.anyio
async def test_gmail_status_returns_configured_without_secret_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_google_oauth(monkeypatch)

    async def fake_exchange(code: str, redirect_uri: str) -> dict[str, object]:
        return {"refresh_token": "refresh-token", "access_token": "access-token", "expires_in": 3600}

    async def fake_profile(access_token: str) -> str:
        return "sender@tenant.test"

    monkeypatch.setattr("app.api.app.exchange_gmail_oauth_code", fake_exchange)
    monkeypatch.setattr("app.api.app.fetch_gmail_profile_email", fake_profile)
    app = create_fastapi_app(db=build_memory_session())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        signup = await _signup(client, tenant_id="tenant-status")
        headers = _auth_headers(signup["token"])
        state = await _oauth_state(client, signup["token"])
        callback = await client.get(
            "/settings/providers/gmail/oauth/callback",
            params={"code": "auth-code", "state": state},
        )
        assert callback.status_code == 302
        status = await client.get("/settings/providers/gmail/status", headers=headers)

    assert status.status_code == 200
    payload = status.json()
    assert payload == {"configured": True, "connected": True, "sender_email": "sender@tenant.test"}
    assert "client_secret" not in payload
    assert "refresh_token" not in payload
    assert "google-client-secret" not in str(payload)


@pytest.mark.anyio
async def test_gmail_status_returns_not_configured() -> None:
    app = create_fastapi_app(db=build_memory_session())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        signup = await _signup(client, tenant_id="tenant-empty")
        status = await client.get("/settings/providers/gmail/status", headers=_auth_headers(signup["token"]))

    assert status.status_code == 200
    assert status.json() == {"configured": False, "connected": False, "sender_email": ""}


@pytest.mark.anyio
async def test_disconnect_clears_gmail_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_google_oauth(monkeypatch)

    async def fake_exchange(code: str, redirect_uri: str) -> dict[str, object]:
        return {"refresh_token": "refresh-token", "access_token": "access-token", "expires_in": 3600}

    async def fake_profile(access_token: str) -> str:
        return "sender@tenant.test"

    monkeypatch.setattr("app.api.app.exchange_gmail_oauth_code", fake_exchange)
    monkeypatch.setattr("app.api.app.fetch_gmail_profile_email", fake_profile)
    db = build_memory_session()
    app = create_fastapi_app(db=db)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        signup = await _signup(client, tenant_id="tenant-disconnect")
        headers = _auth_headers(signup["token"])
        state = await _oauth_state(client, signup["token"])
        await client.get("/settings/providers/gmail/oauth/callback", params={"code": "auth-code", "state": state})
        response = await client.post("/settings/providers/gmail/disconnect", headers=headers)
        status = await client.get("/settings/providers/gmail/status", headers=headers)

    assert response.status_code == 200
    assert response.json() == {"configured": False, "connected": False, "sender_email": ""}
    assert status.json() == {"configured": False, "connected": False, "sender_email": ""}
    tenant = db.tenants.list("tenant-disconnect")[0]
    assert "gmail" not in tenant.settings.get("providers", {})


@pytest.mark.anyio
async def test_free_plan_cannot_open_or_disconnect_gmail_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_google_oauth(monkeypatch)
    app = create_fastapi_app(db=build_memory_session())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        signup = await _signup(client, tenant_id="tenant-free-gmail", plan="Free")
        headers = _auth_headers(signup["token"])
        status = await client.get("/settings/providers/gmail/status", headers=headers)
        start = await client.get("/settings/providers/gmail/oauth/start", headers=headers)
        disconnect = await client.post("/settings/providers/gmail/disconnect", headers=headers)

    assert status.status_code == 403
    assert status.json()["detail"] == "Gmail automation is a Pro feature."
    assert start.status_code == 403
    assert start.json()["detail"] == "Gmail automation is a Pro feature."
    assert disconnect.status_code == 403
    assert disconnect.json()["detail"] == "Gmail automation is a Pro feature."


@pytest.mark.anyio
async def test_outreach_agent_missing_credentials_marks_lead_failed() -> None:
    db = build_memory_session()
    signup_app = create_fastapi_app(db=db)
    transport = httpx.ASGITransport(app=signup_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _signup(client, tenant_id="tenant-no-gmail", plan="Pro")

    tenant = TenantContext(tenant_id="tenant-no-gmail", tenant_slug="tenant-no-gmail")
    lead = db.for_tenant(tenant).save(
        "leads",
        Lead(
            tenant_id=tenant.tenant_id,
            company="No Gmail Co",
            company_url="https://no-gmail.test",
            verified_email="lead@no-gmail.test",
            email="lead@no-gmail.test",
            status="pending",
            outreach_status="pending",
        ),
    )

    result = await OutreachAgent().run(AgentRequest(tenant=tenant, payload={}), db)

    saved = db.for_tenant(tenant).get("leads", lead.id)
    assert result["failed_messages"] == 1
    assert saved.status == "failed"
    assert saved.outreach_status == "failed"
    assert saved.metadata["outreach_error"] == "missing_gmail_credentials"
