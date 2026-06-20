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


class _HealthyProvider:
    async def health_check(self, account):
        return {"email_address": account.email_address}


class _DisabledProvider:
    async def health_check(self, account):
        raise RuntimeError("AccessNotConfigured Gmail API disabled")


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
async def test_gmail_oauth_start_rejects_missing_frontend_url_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_google_oauth(monkeypatch)
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    monkeypatch.setenv("SECRET_KEY", "stable-production-secret-for-tests")
    monkeypatch.setenv("DATABASE_URL", "postgresql://app_user:strong-test-password@db.internal:5432/lead_hunter")
    monkeypatch.delenv("FRONTEND_BASE_URL", raising=False)
    monkeypatch.delenv("APP_FRONTEND_URL", raising=False)
    monkeypatch.setattr("app.configs.settings.settings.frontend_base_url", "")
    monkeypatch.setattr("app.api.app.settings.frontend_base_url", "")
    app = create_fastapi_app(db=build_memory_session())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        signup = await _signup(client, tenant_id="tenant-gmail-prod-missing-frontend")
        response = await client.get("/settings/providers/gmail/oauth/start", headers=_auth_headers(signup["token"]))

    assert response.status_code == 503
    assert response.json()["detail"] == "App URL is not configured. Set FRONTEND_BASE_URL or APP_FRONTEND_URL to your production app URL."


@pytest.mark.anyio
async def test_gmail_oauth_start_rejects_localhost_frontend_url_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_google_oauth(monkeypatch)
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    monkeypatch.setenv("SECRET_KEY", "stable-production-secret-for-tests")
    monkeypatch.setenv("DATABASE_URL", "postgresql://app_user:strong-test-password@db.internal:5432/lead_hunter")
    monkeypatch.setenv("FRONTEND_BASE_URL", "http://127.0.0.1:8501")
    monkeypatch.setattr("app.api.app.settings.frontend_base_url", "")
    app = create_fastapi_app(db=build_memory_session())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        signup = await _signup(client, tenant_id="tenant-gmail-prod-localhost-frontend")
        response = await client.get("/settings/providers/gmail/oauth/start", headers=_auth_headers(signup["token"]))

    assert response.status_code == 503
    assert "localhost" in response.json()["detail"].lower()


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
async def test_gmail_oauth_callback_validates_state_after_new_app_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_google_oauth(monkeypatch)

    async def fake_exchange(code: str, redirect_uri: str) -> dict[str, object]:
        assert code == "auth-code"
        return {"refresh_token": "refresh-token", "access_token": "access-token", "expires_in": 3600}

    async def fake_profile(access_token: str) -> str:
        assert access_token == "access-token"
        return "sender-new-instance@gmail.com"

    monkeypatch.setattr("app.api.app.exchange_gmail_oauth_code", fake_exchange)
    monkeypatch.setattr("app.api.app.fetch_gmail_profile_email", fake_profile)
    db = build_memory_session()
    start_app = create_fastapi_app(db=db)
    start_transport = httpx.ASGITransport(app=start_app)
    async with httpx.AsyncClient(transport=start_transport, base_url="http://testserver") as client:
        signup = await _signup(client, tenant_id="tenant-oauth-new-instance")
        state = await _oauth_state(client, signup["token"])

    callback_app = create_fastapi_app(db=db)
    callback_transport = httpx.ASGITransport(app=callback_app)
    async with httpx.AsyncClient(transport=callback_transport, base_url="http://testserver") as client:
        response = await client.get(
            "/settings/providers/gmail/oauth/callback",
            params={"code": "auth-code", "state": state},
        )

    assert response.status_code == 302
    assert "gmail_oauth=success" in response.headers["location"]
    tenant = db.tenants.list("tenant-oauth-new-instance")[0]
    gmail = tenant.settings["providers"]["gmail"]
    assert gmail["email_address"] == "sender-new-instance@gmail.com"
    assert gmail["connected"] is True


@pytest.mark.anyio
async def test_gmail_oauth_callback_allows_localhost_frontend_in_development(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_google_oauth(monkeypatch)
    monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("FRONTEND_BASE_URL", "http://127.0.0.1:8501")

    async def fake_exchange(code: str, redirect_uri: str) -> dict[str, object]:
        return {"refresh_token": "refresh-token", "access_token": "access-token", "expires_in": 3600}

    async def fake_profile(access_token: str) -> str:
        return "sender-local@gmail.com"

    monkeypatch.setattr("app.api.app.exchange_gmail_oauth_code", fake_exchange)
    monkeypatch.setattr("app.api.app.fetch_gmail_profile_email", fake_profile)
    app = create_fastapi_app(db=build_memory_session())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        signup = await _signup(client, tenant_id="tenant-oauth-localhost")
        state = await _oauth_state(client, signup["token"])
        response = await client.get(
            "/settings/providers/gmail/oauth/callback",
            params={"code": "auth-code", "state": state},
        )

    assert response.status_code == 302
    assert response.headers["location"].startswith("http://127.0.0.1:8501/")
    assert "gmail_oauth=success" in response.headers["location"]


@pytest.mark.anyio
async def test_gmail_oauth_callback_uses_secret_key_when_jwt_secret_is_weak(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_google_oauth(monkeypatch)
    monkeypatch.setenv("JWT_SECRET", "change-me")
    monkeypatch.setenv("SECRET_KEY", "stable-secret-key-for-oauth-state")

    async def fake_exchange(code: str, redirect_uri: str) -> dict[str, object]:
        return {"refresh_token": "refresh-token", "access_token": "access-token", "expires_in": 3600}

    async def fake_profile(access_token: str) -> str:
        return "sender-secret-key@gmail.com"

    monkeypatch.setattr("app.api.app.exchange_gmail_oauth_code", fake_exchange)
    monkeypatch.setattr("app.api.app.fetch_gmail_profile_email", fake_profile)
    db = build_memory_session()
    start_app = create_fastapi_app(db=db)
    start_transport = httpx.ASGITransport(app=start_app)
    async with httpx.AsyncClient(transport=start_transport, base_url="http://testserver") as client:
        signup = await _signup(client, tenant_id="tenant-oauth-secret-key")
        state = await _oauth_state(client, signup["token"])

    callback_app = create_fastapi_app(db=db)
    callback_transport = httpx.ASGITransport(app=callback_app)
    async with httpx.AsyncClient(transport=callback_transport, base_url="http://testserver") as client:
        response = await client.get(
            "/settings/providers/gmail/oauth/callback",
            params={"code": "auth-code", "state": state},
        )

    assert response.status_code == 302
    assert "gmail_oauth=success" in response.headers["location"]


@pytest.mark.anyio
async def test_gmail_oauth_callback_rejects_state_for_missing_user(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_google_oauth(monkeypatch)
    db = build_memory_session()
    app = create_fastapi_app(db=db)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        signup = await _signup(client, tenant_id="tenant-oauth-missing-user")
        state = await _oauth_state(client, signup["token"])

    user = db.users.list("tenant-oauth-missing-user")[0]
    db.users.delete("tenant-oauth-missing-user", user.id)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(
            "/settings/providers/gmail/oauth/callback",
            params={"code": "auth-code", "state": state},
        )

    assert response.status_code == 302
    assert "gmail_oauth=error" in response.headers["location"]
    assert "could+not+be+verified" in response.headers["location"]


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
    monkeypatch.setattr("app.api.app.build_provider_registry", lambda: {"gmail": _HealthyProvider()})
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
    monkeypatch.setattr("app.api.app.build_provider_registry", lambda: {"gmail": _HealthyProvider()})
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
    assert payload["configured"] is True
    assert payload["connected"] is True
    assert payload["sender_email"] == "sender@tenant.test"
    assert payload["status"] == "connected"
    assert payload["status_label"] == "Connected"
    assert payload["last_successful_send"] == ""
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
    payload = status.json()
    assert payload["configured"] is False
    assert payload["connected"] is False
    assert payload["sender_email"] == ""
    assert payload["status"] == "missing_credentials"


@pytest.mark.anyio
async def test_gmail_status_reports_api_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_google_oauth(monkeypatch)

    async def fake_exchange(code: str, redirect_uri: str) -> dict[str, object]:
        return {"refresh_token": "refresh-token", "access_token": "access-token", "expires_in": 3600}

    async def fake_profile(access_token: str) -> str:
        return "sender@tenant.test"

    monkeypatch.setattr("app.api.app.exchange_gmail_oauth_code", fake_exchange)
    monkeypatch.setattr("app.api.app.fetch_gmail_profile_email", fake_profile)
    monkeypatch.setattr("app.api.app.build_provider_registry", lambda: {"gmail": _DisabledProvider()})
    app = create_fastapi_app(db=build_memory_session())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        signup = await _signup(client, tenant_id="tenant-status-api-disabled")
        headers = _auth_headers(signup["token"])
        state = await _oauth_state(client, signup["token"])
        await client.get("/settings/providers/gmail/oauth/callback", params={"code": "auth-code", "state": state})
        status = await client.get("/settings/providers/gmail/status", headers=headers)

    assert status.status_code == 200
    payload = status.json()
    assert payload["configured"] is True
    assert payload["connected"] is False
    assert payload["status"] == "gmail_api_disabled"
    assert payload["status_label"] == "Gmail API disabled"
    assert payload["error"] == "gmail_api_disabled"


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
    status_payload = status.json()
    assert status_payload["configured"] is False
    assert status_payload["connected"] is False
    assert status_payload["sender_email"] == ""
    assert status_payload["status"] == "missing_credentials"
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
    assert saved.metadata["outreach_error"] == "gmail_not_connected"
