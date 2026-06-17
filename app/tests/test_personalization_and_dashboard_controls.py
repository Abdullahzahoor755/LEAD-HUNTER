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


def test_sidebar_brand_html_is_rendered_safely_once() -> None:
    source = dashboard.Path(dashboard.__file__).read_text(encoding="utf-8")

    assert "def render_sidebar_brand()" in source
    assert "st.sidebar.markdown(sidebar_brand_html, unsafe_allow_html=True)" in source
    assert "st.sidebar.write(sidebar_brand_html)" not in source
    assert "st.sidebar.text(sidebar_brand_html)" not in source
    assert "st.sidebar.caption(sidebar_brand_html)" not in source
    assert "st.sidebar.code(sidebar_brand_html)" not in source
    assert "st.code(sidebar_brand_html)" not in source
    assert source.count("st.sidebar.markdown(sidebar_brand_html, unsafe_allow_html=True)") == 1
    assert "lhai-sidebar-brand" in source
    assert "sidebar-brand-title" not in source
    assert "BOLT_LOGO_PATH" in source
    assert "logo-bolt.png" in source
    assert "lhai-sidebar-fallback-logo" not in source
    assert "Lead Hunter AI finds targeted businesses" not in source
    assert "Find better leads" not in source
    assert "Send personalized outreach" not in source
    assert "Track replies and follow-ups" not in source


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
    assert list(dashboard.sort_lead_frame(frame, "Highest score")["company"]) == ["Alpha", "Beta", "Gamma"]


def test_leads_page_is_clean_and_whatsapp_crm_is_separate() -> None:
    source = dashboard.Path(dashboard.__file__).read_text(encoding="utf-8")
    live_start = source.index("def render_live_leads_page")
    live_end = source.index("def render_agency_kit_details")
    live_source = source[live_start:live_end]

    assert "render_whatsapp_crm_row" not in live_source
    assert "whatsapp-message/preview" not in live_source
    assert "AI WhatsApp Message" not in live_source
    assert "def render_whatsapp_crm_page" in source
    assert '"WhatsApp CRM"' in source


def test_whatsapp_crm_table_workflow_exists() -> None:
    source = dashboard.Path(dashboard.__file__).read_text(encoding="utf-8")
    start = source.index("def render_whatsapp_crm_page")
    end = source.index("def render_lead_action_buttons")
    crm_source = source[start:end]

    assert "Manage phone-ready leads and open WhatsApp manually. No auto-sending." in crm_source
    for label in ["Total phone leads", "Valid numbers", "Invalid/missing numbers", "Contacted", "Replied / Interested"]:
        assert label in crm_source
    for label in ["Company", "Website / Domain", "Phone", "Number Valid?", "Score", "Lead Reason", "WhatsApp Status", "Actions"]:
        assert label in crm_source
    for action in ["Generate Message", "Copy Message", "Open WhatsApp", "Mark Contacted", "Mark Replied", "Mark Interested", "Mark Not Interested"]:
        assert action in source
    assert "with st.expander" not in crm_source


def test_whatsapp_preview_is_only_generated_after_button() -> None:
    source = dashboard.Path(dashboard.__file__).read_text(encoding="utf-8")
    row_start = source.index("def render_whatsapp_crm_row")
    row_end = source.index("def render_whatsapp_crm_page")
    row_source = source[row_start:row_end]
    button_index = row_source.index('st.button("Generate Message"')
    preview_index = row_source.index('"/leads/{lead_id}/whatsapp-message/preview"')

    assert button_index < preview_index
    assert "whatsapp_preview_{lead_id}" in row_source


def test_whatsapp_phone_badge_logic() -> None:
    assert dashboard.whatsapp_phone_badge("+92 300 000 0000") == "✅ Valid"
    assert dashboard.whatsapp_phone_badge("") == "⚠️ Missing"
    assert dashboard.whatsapp_phone_badge("123") == "❌ Invalid"
    assert dashboard.whatsapp_phone_is_valid("+923000000000") is True
    assert dashboard.whatsapp_phone_is_valid("123") is False
    assert dashboard.whatsapp_link_for_phone("+92 300 000 0000") == "https://wa.me/923000000000"
    assert dashboard.whatsapp_link_for_phone("+92 300 000 0000", "Hello there") == "https://wa.me/923000000000?text=Hello%20there"
    assert dashboard.whatsapp_link_for_phone("123") == ""


def test_whatsapp_open_button_does_not_require_preview() -> None:
    source = dashboard.Path(dashboard.__file__).read_text(encoding="utf-8")
    row_start = source.index("def render_whatsapp_crm_row")
    row_end = source.index("def render_whatsapp_crm_page")
    row_source = source[row_start:row_end]

    assert "whatsapp_link_for_phone(phone, message)" in row_source
    assert "Invalid or missing phone number." in row_source


def test_leads_filters_are_simple() -> None:
    source = dashboard.Path(dashboard.__file__).read_text(encoding="utf-8")
    controls_start = source.index("def apply_lead_table_controls")
    controls_end = source.index("def sort_lead_frame")
    controls_source = source[controls_start:controls_end]

    assert "Search company/domain/email/phone" in controls_source
    assert "Sort by" in controls_source
    assert "Quick toggle" in controls_source
    assert "Lead quality grade" not in controls_source
    assert "WhatsApp status" not in controls_source
    assert "Has lead reason" not in controls_source


def test_lead_reason_truncates_for_leads_page() -> None:
    long_reason = "This company appears to offer AI and technology services with public contact details and a clear services page for B2B prospects."
    truncated = dashboard.truncate_text(long_reason, 120)

    assert len(truncated) <= 120
    assert truncated.endswith("...")
    assert "search query" not in truncated.lower()


def test_dashboard_save_reason_falls_back_to_service_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResponse:
        is_success = True

        def json(self) -> dict[str, object]:
            return {
                "items": [
                    {
                        "company": "Reason Co",
                        "company_url": "https://reason.test",
                        "service_reason": "Reason Co has verified contact details and clear service fit.",
                        "lead_reason": "",
                        "save_reason": "",
                        "readiness": "research_needed",
                        "readiness_label": "Research Needed",
                    }
                ]
            }

    monkeypatch.setattr(dashboard, "api_request", lambda *args, **kwargs: FakeResponse())

    frame = dashboard.load_dashboard_data(None)  # type: ignore[arg-type]

    assert frame.loc[0, "service_reason"] == "Reason Co has verified contact details and clear service fit."
    assert frame.loc[0, "lead_reason"] == "Reason Co has verified contact details and clear service fit."
    assert frame.loc[0, "save_reason"] == "Reason Co has verified contact details and clear service fit."
    assert frame.loc[0, "readiness"] == "Research Needed"
    assert "readiness" in dashboard.STANDARD_LEAD_EXPORT_COLUMNS


def test_leads_and_whatsapp_views_use_lead_reason_not_save_reason() -> None:
    source = dashboard.Path(dashboard.__file__).read_text(encoding="utf-8")
    leads_start = source.index("def render_live_leads_page")
    leads_end = source.index("def render_settings_page")
    leads_source = source[leads_start:leads_end]
    whatsapp_start = source.index("def render_whatsapp_crm_row")
    whatsapp_end = source.index("def render_whatsapp_crm_page")
    whatsapp_source = source[whatsapp_start:whatsapp_end]

    assert '"lead_reason"' in leads_source
    assert 'row.get("lead_reason", "") or row.get("service_reason", "")' in leads_source
    assert 'row.get("lead_reason", "") or row.get("service_reason", "")' in whatsapp_source
    assert "save_reason" not in leads_source
    assert "save_reason" not in whatsapp_source


def test_email_crm_filters_to_verified_email_and_whatsapp_filters_to_valid_phone() -> None:
    source = dashboard.Path(dashboard.__file__).read_text(encoding="utf-8")
    email_start = source.index('if normalized_page in {"Email CRM", "Email"}')
    email_end = source.index('if normalized_page == "Outreach"')
    email_source = source[email_start:email_end]
    whatsapp_start = source.index("def render_whatsapp_crm_page")
    whatsapp_end = source.index("def render_lead_action_buttons")
    whatsapp_source = source[whatsapp_start:whatsapp_end]

    assert 'frame.get("verified_email"' in email_source
    assert ".str.strip().ne(\"\")" in email_source
    assert 'frame = frame[frame["phone_valid"]].copy()' in whatsapp_source


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
