from __future__ import annotations

import httpx
import pytest

from app.api.app import create_fastapi_app
from app.core.models import Lead, TenantContext
from app.db.session import build_memory_session


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _signup(client: httpx.AsyncClient, tenant_id: str, plan: str = "Agency") -> dict[str, str]:
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


def _lead(db, tenant_id: str, **kwargs) -> Lead:
    lead = Lead(tenant_id=tenant_id, **kwargs)
    return db.for_tenant(TenantContext(tenant_id=tenant_id)).save("leads", lead)


@pytest.mark.anyio
async def test_campaign_from_idea_works_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    db = build_memory_session()
    app = create_fastapi_app(db=db)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        signup = await _signup(client, "tenant-marketing-idea")
        response = await client.post(
            "/marketing/campaign/from-idea",
            headers=_auth_headers(signup["token"]),
            json={
                "business_idea": "immigration consultancy for Canada visas",
                "target_location": "Dubai, UAE",
                "target_audience": "working professionals planning to move abroad",
                "campaign_goal": "book consultation calls",
            },
        )

    assert response.status_code == 200
    campaign = response.json()["marketing_campaign_kit"]
    assert campaign["mode"] == "fallback"
    assert campaign["campaign_goal"] == "book consultation calls"
    assert "Facebook/Instagram" in campaign["recommended_platforms"]
    assert campaign["facebook_instagram_ads"]
    assert campaign["google_search_ads"]
    assert campaign["seven_day_content_calendar"]


@pytest.mark.anyio
async def test_campaign_from_lead_works_without_api_key_and_stores_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    db = build_memory_session()
    app = create_fastapi_app(db=db)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        signup = await _signup(client, "tenant-marketing-lead")
        lead = _lead(
            db,
            "tenant-marketing-lead",
            industry="Restaurant",
            country="UAE",
            company_url="https://food.test",
            score=72,
            service_reason="needs more local orders",
            metadata={"agency_kit": {"recommended_service": "Google Maps optimization + WhatsApp ordering funnel"}},
        )
        response = await client.post(
            f"/marketing/campaign/from-lead/{lead.id}",
            headers=_auth_headers(signup["token"]),
            json={},
        )

    assert response.status_code == 200
    campaign = response.json()["marketing_campaign_kit"]
    assert campaign["mode"] == "fallback"
    assert "food.test" in campaign["business_idea"]
    saved = db.for_tenant(TenantContext(tenant_id="tenant-marketing-lead")).get("leads", lead.id)
    assert saved is not None
    assert saved.metadata["marketing_campaign_kit"]["mode"] == "fallback"
    assert saved.metadata["marketing_campaign_kit"]["reels_tiktok_script"]["hook"]


@pytest.mark.anyio
async def test_unknown_category_gets_useful_default_campaign() -> None:
    db = build_memory_session()
    app = create_fastapi_app(db=db)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        signup = await _signup(client, "tenant-marketing-default")
        response = await client.post(
            "/marketing/campaign/from-idea",
            headers=_auth_headers(signup["token"]),
            json={"business_idea": "artisan pottery subscription club"},
        )

    assert response.status_code == 200
    campaign = response.json()["marketing_campaign_kit"]
    assert campaign["lead_magnet"] == "Free growth checklist"
    assert campaign["landing_page_copy"]["headline"]
    assert campaign["next_action"]


@pytest.mark.anyio
async def test_tenant_isolation_enforced_for_lead_campaign() -> None:
    db = build_memory_session()
    app = create_fastapi_app(db=db)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        signup_one = await _signup(client, "tenant-marketing-one")
        signup_two = await _signup(client, "tenant-marketing-two")
        lead = _lead(db, "tenant-marketing-one", industry="Real Estate", company_url="https://property.test")

        forbidden = await client.post(
            f"/marketing/campaign/from-lead/{lead.id}",
            headers=_auth_headers(signup_two["token"]),
            json={},
        )
        allowed = await client.post(
            f"/marketing/campaign/from-lead/{lead.id}",
            headers=_auth_headers(signup_one["token"]),
            json={},
        )

    assert forbidden.status_code == 404
    assert allowed.status_code == 200
    saved = db.for_tenant(TenantContext(tenant_id="tenant-marketing-one")).get("leads", lead.id)
    assert saved is not None
    assert saved.metadata["marketing_campaign_kit"]["mode"] == "fallback"
