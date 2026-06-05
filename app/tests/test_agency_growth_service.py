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
async def test_offer_match_works_without_api_key_and_stores_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    db = build_memory_session()
    app = create_fastapi_app(db=db)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        signup = await _signup(client, "tenant-growth-offer")
        lead = _lead(
            db,
            "tenant-growth-offer",
            industry="Restaurant",
            country="UAE",
            company_url="https://food.test",
            score=70,
            metadata={"phone": "+971500000000"},
        )
        response = await client.post(
            f"/leads/{lead.id}/offer-match",
            headers=_auth_headers(signup["token"]),
            json={},
        )

    assert response.status_code == 200
    offer_match = response.json()["offer_match"]
    assert offer_match["mode"] == "fallback"
    assert offer_match["recommended_offer"] == "Google Maps optimization + WhatsApp ordering funnel"
    assert offer_match["offer_category"] == "google_maps"
    assert offer_match["best_channel"] == "whatsapp"
    saved = db.for_tenant(TenantContext(tenant_id="tenant-growth-offer")).get("leads", lead.id)
    assert saved is not None
    assert saved.metadata["offer_match"]["recommended_offer"] == offer_match["recommended_offer"]


@pytest.mark.anyio
async def test_whatsapp_sales_kit_uses_phone_rule_and_stores_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    db = build_memory_session()
    app = create_fastapi_app(db=db)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        signup = await _signup(client, "tenant-growth-whatsapp")
        lead = _lead(
            db,
            "tenant-growth-whatsapp",
            industry="Clinic",
            company_url="https://clinic.test",
            metadata={
                "contact": {"phone": "+923000000000"},
                "offer_match": {"recommended_offer": "Appointment booking page + patient lead form", "business_pain": ["missed appointment inquiries"]},
            },
        )
        response = await client.post(
            f"/leads/{lead.id}/whatsapp-sales-kit",
            headers=_auth_headers(signup["token"]),
            json={},
        )

    assert response.status_code == 200
    sales_kit = response.json()["whatsapp_sales_kit"]
    assert sales_kit["mode"] == "fallback"
    assert sales_kit["recommended_channel"] == "whatsapp"
    assert sales_kit["whatsapp_opener"]
    assert sales_kit["call_script"]["soft_close"]
    saved = db.for_tenant(TenantContext(tenant_id="tenant-growth-whatsapp")).get("leads", lead.id)
    assert saved is not None
    assert saved.metadata["whatsapp_sales_kit"]["recommended_channel"] == "whatsapp"


@pytest.mark.anyio
async def test_unknown_offer_match_gets_useful_default() -> None:
    db = build_memory_session()
    app = create_fastapi_app(db=db)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        signup = await _signup(client, "tenant-growth-default")
        lead = _lead(db, "tenant-growth-default", industry="Unclassified", score=20)
        response = await client.post(
            f"/leads/{lead.id}/offer-match",
            headers=_auth_headers(signup["token"]),
            json={},
        )

    assert response.status_code == 200
    offer_match = response.json()["offer_match"]
    assert offer_match["recommended_offer"] == "Website audit + lead capture system"
    assert offer_match["offer_category"] == "website"
    assert offer_match["confidence_score"] >= 30


@pytest.mark.anyio
async def test_mini_agency_plan_works_without_api_key_and_requires_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    db = build_memory_session()
    app = create_fastapi_app(db=db)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        unauthenticated = await client.post(
            "/agency/mini-agency-plan",
            json={"skill": "web design", "target_country": "UAE", "target_city": "Dubai"},
        )
        signup = await _signup(client, "tenant-growth-mini")
        response = await client.post(
            "/agency/mini-agency-plan",
            headers=_auth_headers(signup["token"]),
            json={
                "skill": "web design",
                "target_country": "UAE",
                "target_city": "Dubai",
                "daily_time": "1 hour",
                "goal": "first client",
                "preferred_niche": "clinics",
            },
        )

    assert unauthenticated.status_code == 401
    assert response.status_code == 200
    plan = response.json()["mini_agency_plan"]
    assert plan["mode"] == "fallback"
    assert "clinics" in plan["best_niches"]
    assert len(plan["daily_roadmap"]) == 14
    assert plan["outreach_scripts"]["whatsapp"]


@pytest.mark.anyio
async def test_tenant_isolation_for_offer_match_and_whatsapp_sales_kit() -> None:
    db = build_memory_session()
    app = create_fastapi_app(db=db)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        signup_one = await _signup(client, "tenant-growth-one")
        signup_two = await _signup(client, "tenant-growth-two")
        lead = _lead(db, "tenant-growth-one", industry="Real Estate", company_url="https://property.test")

        forbidden_offer = await client.post(
            f"/leads/{lead.id}/offer-match",
            headers=_auth_headers(signup_two["token"]),
            json={},
        )
        forbidden_whatsapp = await client.post(
            f"/leads/{lead.id}/whatsapp-sales-kit",
            headers=_auth_headers(signup_two["token"]),
            json={},
        )
        allowed = await client.post(
            f"/leads/{lead.id}/offer-match",
            headers=_auth_headers(signup_one["token"]),
            json={},
        )

    assert forbidden_offer.status_code == 404
    assert forbidden_whatsapp.status_code == 404
    assert allowed.status_code == 200
    saved = db.for_tenant(TenantContext(tenant_id="tenant-growth-one")).get("leads", lead.id)
    assert saved is not None
    assert saved.metadata["offer_match"]["recommended_offer"] == "Property lead capture landing page + WhatsApp follow-up"
    assert "whatsapp_sales_kit" not in saved.metadata
