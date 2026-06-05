from __future__ import annotations

import httpx
import pytest

from app.api.app import create_fastapi_app
from app.core.models import Lead, Tenant, TenantContext
from app.db.session import build_memory_session
from app.services.agency_kit_service import AgencyKitLimitError, AgencyKitService


def _tenant(db, tenant_id: str = "tenant-agency", plan: str = "Agency") -> TenantContext:
    db.tenants.save(Tenant(tenant_id=tenant_id, name=tenant_id, slug=tenant_id, subscription_plan=plan))
    return TenantContext(tenant_id=tenant_id, tenant_slug=tenant_id)


def _lead(db, tenant: TenantContext, **kwargs) -> Lead:
    lead = Lead(tenant_id=tenant.tenant_id, **kwargs)
    return db.for_tenant(tenant).save("leads", lead)


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


@pytest.mark.anyio
async def test_agency_kit_works_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    db = build_memory_session()
    tenant = _tenant(db)
    lead = _lead(db, tenant, industry="Technology", company_url="https://tech.test", verified_email="hello@tech.test")

    kit = await AgencyKitService(db).generate_for_lead(tenant, lead.id)

    assert kit["mode"] == "fallback"
    assert kit["recommended_service"] == "Lead generation system + automation funnel"
    assert "outreach_email" in kit


@pytest.mark.anyio
async def test_verified_email_gets_email_channel() -> None:
    db = build_memory_session()
    tenant = _tenant(db, "tenant-email")
    lead = _lead(db, tenant, industry="Software", verified_email="lead@example.test", score=60)

    kit = await AgencyKitService(db).generate_for_lead(tenant, lead.id)

    assert kit["recommended_channel"] == "email"


@pytest.mark.anyio
async def test_phone_metadata_gets_phone_or_whatsapp_strategy() -> None:
    db = build_memory_session()
    tenant = _tenant(db, "tenant-phone")
    lead = _lead(db, tenant, industry="Restaurant", metadata={"phone": "+971500000000"})

    kit = await AgencyKitService(db).generate_for_lead(tenant, lead.id)

    assert kit["recommended_channel"] in {"phone", "whatsapp"}
    assert "WhatsApp" in kit["whatsapp_or_call_script"] or "send" in kit["next_action"].lower()


@pytest.mark.anyio
async def test_url_without_contact_gets_website_form_strategy() -> None:
    db = build_memory_session()
    tenant = _tenant(db, "tenant-url")
    lead = _lead(db, tenant, company_url="https://clinic.test", industry="Clinic")

    kit = await AgencyKitService(db).generate_for_lead(tenant, lead.id)

    assert kit["recommended_channel"] == "website_form"


@pytest.mark.anyio
async def test_unknown_industry_gets_default_useful_kit() -> None:
    db = build_memory_session()
    tenant = _tenant(db, "tenant-default")
    lead = _lead(db, tenant, industry="Unclassified", score=20)

    kit = await AgencyKitService(db).generate_for_lead(tenant, lead.id)

    assert kit["recommended_service"] == "Website audit + local lead capture system"
    assert kit["confidence_score"] >= 35
    assert kit["next_action"]


@pytest.mark.anyio
async def test_metadata_update_preserves_existing_metadata() -> None:
    db = build_memory_session()
    tenant = _tenant(db, "tenant-metadata")
    lead = _lead(db, tenant, industry="Education", metadata={"source": "manual", "phone": "+923000000000"})

    await AgencyKitService(db).generate_for_lead(tenant, lead.id)

    saved = db.for_tenant(tenant).get("leads", lead.id)
    assert saved.metadata["source"] == "manual"
    assert saved.metadata["phone"] == "+923000000000"
    assert saved.metadata["agency_kit"]["recommended_service"] == "Admissions lead funnel + WhatsApp nurture sequence"


@pytest.mark.anyio
async def test_tenant_isolation_is_enforced() -> None:
    db = build_memory_session()
    tenant_one = _tenant(db, "tenant-one")
    tenant_two = _tenant(db, "tenant-two")
    lead = _lead(db, tenant_one, industry="Real Estate", verified_email="lead@property.test")

    with pytest.raises(ValueError):
        await AgencyKitService(db).generate_for_lead(tenant_two, lead.id)

    saved = db.for_tenant(tenant_one).get("leads", lead.id)
    assert "agency_kit" not in saved.metadata


@pytest.mark.anyio
async def test_free_limit_behavior() -> None:
    db = build_memory_session()
    tenant = _tenant(db, "tenant-free-limit", plan="Free")
    leads = [_lead(db, tenant, industry="Retail", company_url=f"https://shop{index}.test") for index in range(4)]
    service = AgencyKitService(db)

    for lead in leads[:3]:
        await service.generate_for_lead(tenant, lead.id)

    with pytest.raises(AgencyKitLimitError):
        await service.generate_for_lead(tenant, leads[3].id)


@pytest.mark.anyio
async def test_agency_kit_endpoint_updates_lead_metadata_and_stays_tenant_scoped() -> None:
    db = build_memory_session()
    app = create_fastapi_app(db=db)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        signup_one = await _signup(client, "tenant-api-one")
        signup_two = await _signup(client, "tenant-api-two")
        tenant_one = TenantContext(tenant_id="tenant-api-one")
        lead = _lead(db, tenant_one, industry="Construction", company_url="https://builder.test")

        forbidden = await client.post(
            f"/leads/{lead.id}/agency-kit",
            headers=_auth_headers(signup_two["token"]),
            json={},
        )
        response = await client.post(
            f"/leads/{lead.id}/agency-kit",
            headers=_auth_headers(signup_one["token"]),
            json={},
        )

    assert forbidden.status_code == 404
    assert response.status_code == 200
    payload = response.json()
    assert payload["agency_kit"]["mode"] == "fallback"
    saved = db.for_tenant(tenant_one).get("leads", lead.id)
    assert saved.metadata["agency_kit"]["recommended_channel"] == "website_form"
