from __future__ import annotations

import json

import httpx
import pytest

from app.api.app import create_fastapi_app
from app.core.models import Lead, Tenant, TenantContext
from app.db.session import build_memory_session
from app.services.agency_kit_service import AgencyKitService
from app.services.marketing_campaign_service import MarketingCampaignService


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _signup(client: httpx.AsyncClient, tenant_id: str = "tenant-ai", plan: str = "Pro") -> dict[str, str]:
    response = await client.post(
        "/signup",
        json={
            "tenant_id": tenant_id,
            "tenant_name": tenant_id,
            "tenant_slug": tenant_id,
            "email": f"owner@{tenant_id}.test",
            "password": "secret123",
            "full_name": "Owner",
            "plan": plan,
        },
    )
    assert response.status_code == 200
    return response.json()


def _tenant(db, tenant_id: str = "tenant-ai-service", plan: str = "Agency") -> TenantContext:
    db.tenants.save(Tenant(tenant_id=tenant_id, name=tenant_id, slug=tenant_id, subscription_plan=plan))
    return TenantContext(tenant_id=tenant_id, tenant_slug=tenant_id)


def _lead(db, tenant: TenantContext, **kwargs) -> Lead:
    lead = Lead(tenant_id=tenant.tenant_id, **kwargs)
    return db.for_tenant(tenant).save("leads", lead)


@pytest.mark.anyio
async def test_ai_provider_settings_save_without_exposing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.configs.settings.settings.secret_encryption_key", "test-key")
    db = build_memory_session()
    app = create_fastapi_app(db=db)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        signup = await _signup(client, "tenant-ai-save")
        response = await client.post(
            "/settings/providers/ai",
            headers=_auth_headers(signup["token"]),
            json={"provider": "openai", "api_key": "sk-test-secret", "model": "gpt-4o-mini", "enabled": True},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload == {"configured": True, "provider": "openai", "model": "gpt-4o-mini", "enabled": True}
    assert "sk-test-secret" not in json.dumps(payload)
    tenant = db.tenants.list("tenant-ai-save")[0]
    ai_settings = tenant.settings["providers"]["ai"]
    assert ai_settings["api_key_encrypted"] != "sk-test-secret"
    assert "sk-test-secret" not in json.dumps(ai_settings)


@pytest.mark.anyio
async def test_ai_provider_status_does_not_return_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.configs.settings.settings.secret_encryption_key", "test-key")
    app = create_fastapi_app(db=build_memory_session())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        signup = await _signup(client, "tenant-ai-status")
        headers = _auth_headers(signup["token"])
        save = await client.post(
            "/settings/providers/ai",
            headers=headers,
            json={"provider": "groq", "api_key": "gsk-secret", "model": "llama-3.1-8b-instant", "enabled": True},
        )
        assert save.status_code == 200
        status = await client.get("/settings/providers/ai/status", headers=headers)

    assert status.status_code == 200
    payload = status.json()
    assert payload == {"configured": True, "provider": "groq", "model": "llama-3.1-8b-instant", "enabled": True}
    assert "api_key" not in payload
    assert "gsk-secret" not in json.dumps(payload)


@pytest.mark.anyio
async def test_fallback_provider_requires_no_key() -> None:
    app = create_fastapi_app(db=build_memory_session())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        signup = await _signup(client, "tenant-ai-fallback")
        headers = _auth_headers(signup["token"])
        response = await client.post(
            "/settings/providers/ai",
            headers=headers,
            json={"provider": "fallback", "api_key": "", "model": "", "enabled": True},
        )
        status = await client.get("/settings/providers/ai/status", headers=headers)

    assert response.status_code == 200
    assert status.status_code == 200
    assert status.json() == {"configured": True, "provider": "fallback", "model": "", "enabled": True}


@pytest.mark.anyio
async def test_invalid_ai_provider_is_rejected() -> None:
    app = create_fastapi_app(db=build_memory_session())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        signup = await _signup(client, "tenant-ai-invalid")
        response = await client.post(
            "/settings/providers/ai",
            headers=_auth_headers(signup["token"]),
            json={"provider": "made-up-ai", "api_key": "secret", "model": "model", "enabled": True},
        )

    assert response.status_code == 400
    assert "Unsupported AI provider" in response.json()["detail"]


@pytest.mark.anyio
async def test_provider_test_endpoint_handles_failure_safely(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.configs.settings.settings.secret_encryption_key", "test-key")

    async def fail_generate(*args, **kwargs):
        raise RuntimeError("boom with secret")

    monkeypatch.setattr("app.services.ai_provider_service.AIProviderService.generate_text", fail_generate)
    app = create_fastapi_app(db=build_memory_session())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        signup = await _signup(client, "tenant-ai-test-failure")
        headers = _auth_headers(signup["token"])
        save = await client.post(
            "/settings/providers/ai",
            headers=headers,
            json={"provider": "openai", "api_key": "sk-failure-secret", "model": "gpt-4o-mini", "enabled": True},
        )
        assert save.status_code == 200
        response = await client.post("/settings/providers/ai/test", headers=headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is False
    assert payload["message"] == "AI provider test failed safely."
    assert "sk-failure-secret" not in json.dumps(payload)
    assert "boom" not in json.dumps(payload)


@pytest.mark.anyio
async def test_existing_agency_kit_still_falls_back_without_provider() -> None:
    db = build_memory_session()
    tenant = _tenant(db, "tenant-ai-agency-fallback")
    lead = _lead(db, tenant, industry="Restaurant", company_url="https://food.test")

    kit = await AgencyKitService(db).generate_for_lead(tenant, lead.id)

    assert kit["mode"] == "fallback"
    assert kit["recommended_service"] == "Google Maps optimization + WhatsApp ordering funnel"


@pytest.mark.anyio
async def test_existing_marketing_kit_still_falls_back_without_provider() -> None:
    db = build_memory_session()
    tenant = _tenant(db, "tenant-ai-marketing-fallback")

    campaign = await MarketingCampaignService(db).generate_from_idea(tenant, "clinic appointment ads")

    assert campaign["mode"] == "fallback"
    assert campaign["facebook_instagram_ads"]
