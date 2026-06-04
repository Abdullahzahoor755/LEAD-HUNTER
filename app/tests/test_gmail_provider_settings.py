from __future__ import annotations

import httpx
import pytest

from app.agents.base import AgentRequest
from app.agents.outreach import OutreachAgent
from app.api.app import create_fastapi_app
from app.core.models import TenantContext
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


@pytest.mark.anyio
async def test_save_gmail_credentials_stores_tenant_provider_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.configs.settings.settings.secret_encryption_key", "test-key")
    db = build_memory_session()
    app = create_fastapi_app(db=db)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        signup = await _signup(client)
        response = await client.post(
            "/settings/providers/gmail",
            headers=_auth_headers(signup["token"]),
            json={
                "client_id": "client-id",
                "client_secret": "client-secret",
                "refresh_token": "refresh-token",
                "sender_email": "Sender@Tenant.test",
            },
        )

    assert response.status_code == 200
    tenant = db.tenants.list("tenant-gmail")[0]
    gmail = tenant.settings["providers"]["gmail"]
    assert gmail["client_id"] == "client-id"
    assert gmail["client_secret"] == "client-secret"
    assert gmail["email_address"] == "sender@tenant.test"
    assert gmail["refresh_token_encrypted"] != "refresh-token"


@pytest.mark.anyio
async def test_gmail_status_returns_configured_without_secret_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.configs.settings.settings.secret_encryption_key", "test-key")
    app = create_fastapi_app(db=build_memory_session())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        signup = await _signup(client, tenant_id="tenant-status")
        headers = _auth_headers(signup["token"])
        save = await client.post(
            "/settings/providers/gmail",
            headers=headers,
            json={
                "client_id": "client-id",
                "client_secret": "client-secret",
                "refresh_token": "refresh-token",
                "sender_email": "sender@tenant.test",
            },
        )
        assert save.status_code == 200

        status = await client.get("/settings/providers/gmail/status", headers=headers)

    assert status.status_code == 200
    payload = status.json()
    assert payload == {"configured": True, "sender_email": "sender@tenant.test"}
    assert "client_secret" not in payload
    assert "refresh_token" not in payload


@pytest.mark.anyio
async def test_gmail_status_returns_not_configured() -> None:
    app = create_fastapi_app(db=build_memory_session())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        signup = await _signup(client, tenant_id="tenant-empty")
        status = await client.get("/settings/providers/gmail/status", headers=_auth_headers(signup["token"]))

    assert status.status_code == 200
    assert status.json() == {"configured": False, "sender_email": ""}


@pytest.mark.anyio
async def test_free_plan_cannot_open_or_save_gmail_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.configs.settings.settings.secret_encryption_key", "test-key")
    app = create_fastapi_app(db=build_memory_session())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        signup = await _signup(client, tenant_id="tenant-free-gmail", plan="Free")
        headers = _auth_headers(signup["token"])
        status = await client.get("/settings/providers/gmail/status", headers=headers)
        save = await client.post(
            "/settings/providers/gmail",
            headers=headers,
            json={
                "client_id": "client-id",
                "client_secret": "client-secret",
                "refresh_token": "refresh-token",
                "sender_email": "sender@tenant.test",
            },
        )

    assert status.status_code == 403
    assert status.json()["detail"] == "Gmail automation is a Pro feature."
    assert save.status_code == 403
    assert save.json()["detail"] == "Gmail automation is a Pro feature."


@pytest.mark.anyio
async def test_outreach_agent_missing_credentials_error_remains_clear() -> None:
    db = build_memory_session()
    signup_app = create_fastapi_app(db=db)
    transport = httpx.ASGITransport(app=signup_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _signup(client, tenant_id="tenant-no-gmail", plan="Pro")

    tenant = TenantContext(tenant_id="tenant-no-gmail", tenant_slug="tenant-no-gmail")
    with pytest.raises(ValueError, match="Tenant Gmail credentials are not configured."):
        await OutreachAgent().run(AgentRequest(tenant=tenant, payload={}), db)
