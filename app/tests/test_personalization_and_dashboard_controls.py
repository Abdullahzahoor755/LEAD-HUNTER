from __future__ import annotations

import httpx
import pandas as pd
import pytest

import dashboard
from app.api.app import create_fastapi_app
from app.db.session import build_memory_session


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _signup(client: httpx.AsyncClient, tenant_id: str) -> dict[str, str]:
    response = await client.post(
        "/signup",
        json={
            "tenant_id": tenant_id,
            "tenant_name": tenant_id,
            "tenant_slug": tenant_id,
            "email": f"owner@{tenant_id}.test",
            "password": "secret123",
            "full_name": "Tenant Owner",
            "plan": "Pro",
        },
    )
    assert response.status_code == 200
    return response.json()


def test_sidebar_css_contains_open_and_close_selectors() -> None:
    source = dashboard.Path(dashboard.__file__).read_text(encoding="utf-8")

    assert '[data-testid="stSidebarCollapseButton"]' in source
    assert '[data-testid="collapsedControl"]' in source
    assert 'button[title="Collapse sidebar"]' in source
    assert 'button[title="Open sidebar"]' in source
    assert 'button[aria-label="Collapse sidebar"]' in source
    assert 'button[aria-label="Open sidebar"]' in source
    assert "background: #07111f" in source


def test_lead_sort_newest_oldest_and_highest_score() -> None:
    frame = pd.DataFrame(
        [
            {"company": "Beta", "score": 20, "created_at": "2026-01-02T00:00:00+00:00"},
            {"company": "Alpha", "score": 90, "created_at": "2026-01-03T00:00:00+00:00"},
            {"company": "Gamma", "score": 10, "created_at": "2026-01-01T00:00:00+00:00"},
        ]
    )

    assert list(dashboard.sort_lead_frame(frame, "Newest first")["company"]) == ["Alpha", "Beta", "Gamma"]
    assert list(dashboard.sort_lead_frame(frame, "Oldest first")["company"]) == ["Gamma", "Beta", "Alpha"]
    assert list(dashboard.sort_lead_frame(frame, "Highest score first")["company"]) == ["Alpha", "Beta", "Gamma"]


def test_lead_sort_quality_email_ready_and_company() -> None:
    frame = pd.DataFrame(
        [
            {"company": "Zulu", "lead_quality_grade": "C", "verified_email": ""},
            {"company": "Alpha", "lead_quality_grade": "A", "verified_email": "lead@alpha.test"},
            {"company": "Beta", "lead_quality_grade": "B", "verified_email": ""},
        ]
    )

    assert list(dashboard.sort_lead_frame(frame, "A-grade first")["company"]) == ["Alpha", "Beta", "Zulu"]
    assert list(dashboard.sort_lead_frame(frame, "Email available first")["company"])[0] == "Alpha"
    assert list(dashboard.sort_lead_frame(frame, "Company name A-Z")["company"]) == ["Alpha", "Beta", "Zulu"]


@pytest.mark.anyio
async def test_outreach_profile_saves_per_tenant_and_preview_uses_config() -> None:
    app = create_fastapi_app(db=build_memory_session())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        first = await _signup(client, "tenant-profile-one")
        second = await _signup(client, "tenant-profile-two")
        first_headers = _auth_headers(first["token"])
        second_headers = _auth_headers(second["token"])

        payload = {
            "sender_name": "Abdullah Zahoor",
            "brand_name": "Lead Hunter AI",
            "services_offered": "AI lead generation and website automation",
            "target_customer_type": "software houses",
            "tone": "Warm",
            "email_goal": "Book a meeting",
            "cta": "a quick 10-minute call this week",
            "language": "English",
            "signature": "Abdullah\nFounder, Lead Hunter AI",
        }
        save = await client.post("/settings/outreach-profile", json=payload, headers=first_headers)
        assert save.status_code == 200
        assert save.json()["config"]["sender_name"] == "Abdullah Zahoor"

        first_profile = await client.get("/settings/outreach-profile", headers=first_headers)
        second_profile = await client.get("/settings/outreach-profile", headers=second_headers)
        assert first_profile.json()["config"]["brand_name"] == "Lead Hunter AI"
        assert second_profile.json()["config"]["brand_name"] == ""

        preview = await client.post("/outreach/preview-email", json=payload, headers=first_headers)
        assert preview.status_code == 200
        sample = preview.json()["sample"]
        assert "Abdullah Zahoor" in sample["body"]
        assert "AI lead generation and website automation" in sample["body"]
        assert sample["tone"] == "Warm"


@pytest.mark.anyio
async def test_outreach_preview_endpoint_does_not_send_email() -> None:
    db = build_memory_session()
    app = create_fastapi_app(db=db)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        signup = await _signup(client, "tenant-preview-no-send")
        response = await client.post(
            "/outreach/preview-email",
            json={"sender_name": "Sender", "services_offered": "automation"},
            headers=_auth_headers(signup["token"]),
        )

    assert response.status_code == 200
    assert db.emails.list("tenant-preview-no-send") == []
