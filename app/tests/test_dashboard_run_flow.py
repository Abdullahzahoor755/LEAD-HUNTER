from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest


@dataclass
class _FakeResponse:
    payload: dict[str, Any]
    is_success: bool = True
    status_code: int = 200
    headers: dict[str, str] | None = None

    @property
    def text(self) -> str:
        return "{}"

    def json(self) -> dict[str, Any]:
        return self.payload


class _NullContext:
    def __enter__(self) -> "_NullContext":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


class _TabsContext:
    def __init__(self) -> None:
        self.items = (_NullContext(), _NullContext())

    def __iter__(self):
        return iter(self.items)


class _FakeStreamlit:
    def __init__(self) -> None:
        self.session_state: dict[str, Any] = {}
        self.text_input_calls: list[tuple[str, dict[str, Any]]] = []
        self.rerun_count = 0
        self.sidebar = _FakeSidebar(self)

    def markdown(self, *args: Any, **kwargs: Any) -> None:
        return None

    def columns(self, *args: Any, **kwargs: Any):
        return _NullContext(), _NullContext()

    def tabs(self, *args: Any, **kwargs: Any):
        return _TabsContext()

    def form(self, *args: Any, **kwargs: Any):
        return _NullContext()

    def text_input(self, label: str, **kwargs: Any) -> str:
        self.text_input_calls.append((label, kwargs))
        return ""

    def form_submit_button(self, *args: Any, **kwargs: Any) -> bool:
        return False

    def error(self, *args: Any, **kwargs: Any) -> None:
        return None

    def rerun(self) -> None:
        self.rerun_count += 1

    def button(self, *args: Any, **kwargs: Any) -> bool:
        return False

    def caption(self, *args: Any, **kwargs: Any) -> None:
        return None

    def progress(self, *args: Any, **kwargs: Any) -> None:
        return None

    def success(self, *args: Any, **kwargs: Any) -> None:
        return None

    def info(self, *args: Any, **kwargs: Any) -> None:
        return None

    def dataframe(self, *args: Any, **kwargs: Any) -> None:
        return None


class _FakeSidebar:
    def __init__(self, app: _FakeStreamlit) -> None:
        self.app = app
        self.radio_calls: list[tuple[str, list[str], dict[str, Any]]] = []

    def markdown(self, *args: Any, **kwargs: Any) -> None:
        return None

    def button(self, *args: Any, **kwargs: Any) -> bool:
        return False

    def checkbox(self, *args: Any, **kwargs: Any) -> bool:
        return False

    def radio(self, label: str, options: list[str], **kwargs: Any) -> str:
        choices = list(options)
        self.radio_calls.append((label, choices, kwargs))
        key = str(kwargs.get("key", "") or "")
        selected = self.app.session_state.get(key)
        if selected not in choices:
            selected = choices[0]
            if key:
                self.app.session_state[key] = selected
        return str(selected)


def test_dashboard_run_once_sends_job_type(monkeypatch) -> None:
    import dashboard

    calls: list[tuple[str, str, dict[str, Any]]] = []

    def fake_api_request(method: str, path: str, **kwargs: Any) -> _FakeResponse:
        calls.append((method, path, kwargs))
        return _FakeResponse(payload={"status": "completed"})

    monkeypatch.setattr(dashboard, "api_request", fake_api_request)

    dashboard.enqueue_job("lead_generation", {"limit": 10}, run_now=True)
    dashboard.enqueue_job("outreach", run_now=True)

    run_calls = [call for call in calls if call[1] == "/jobs/run-once"]
    assert run_calls[0][2]["json"] == {"job_type": "lead_generation"}
    assert run_calls[1][2]["json"] == {"job_type": "outreach"}


def test_start_lead_generation_enqueues_without_run_once(monkeypatch) -> None:
    import dashboard

    fake_st = _FakeStreamlit()
    monkeypatch.setattr(dashboard, "st", fake_st)
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def fake_api_request(method: str, path: str, **kwargs: Any) -> _FakeResponse:
        calls.append((method, path, kwargs))
        return _FakeResponse(payload={"job_id": "job-123", "tenant_id": "tenant-ui", "agent_name": "lead_generation"})

    monkeypatch.setattr(dashboard, "api_request", fake_api_request)

    dashboard.start_lead_generation_job({"query": "software companies", "limit": 10})

    assert fake_st.session_state["active_lead_generation_job_id"] == "job-123"
    assert [call[1] for call in calls] == ["/jobs"]
    assert calls[0][2]["json"]["agent_name"] == "lead_generation"


def test_completed_lead_generation_job_triggers_dashboard_refresh(monkeypatch) -> None:
    import dashboard

    fake_st = _FakeStreamlit()
    fake_st.session_state["active_lead_generation_job_id"] = "job-completed"
    fake_st.session_state["lead_generation_busy"] = True
    monkeypatch.setattr(dashboard, "st", fake_st)

    def fake_api_request(method: str, path: str, **kwargs: Any) -> _FakeResponse:
        if path == "/jobs/job-completed/status":
            return _FakeResponse(
                payload={
                    "job_id": "job-completed",
                    "status": "completed",
                    "progress_percentage": 100,
                    "result": {"saved_leads": 1},
                }
            )
        if path == "/jobs/job-completed/events":
            return _FakeResponse(payload={"events": []})
        raise AssertionError(f"unexpected request {method} {path}")

    monkeypatch.setattr(dashboard, "api_request", fake_api_request)

    active = dashboard.render_active_lead_generation_job()

    assert active is False
    assert fake_st.session_state["active_lead_generation_job_id"] == ""
    assert fake_st.session_state["lead_generation_busy"] is False
    assert fake_st.rerun_count == 1


@pytest.mark.anyio
async def test_stale_lead_generation_job_does_not_block_new_enqueue() -> None:
    import httpx

    from app.api.app import create_fastapi_app
    from app.core.auth import create_jwt_token
    from app.core.models import Job, Tenant
    from app.core.models import utc_now
    from app.db.session import build_memory_session

    db = build_memory_session()
    tenant = Tenant(tenant_id="tenant-stale-job", subscription_plan="Free")
    db.tenants.save(tenant)
    stale_job = Job(
        tenant_id=tenant.tenant_id,
        name="lead_generation",
        status="queued",
        payload={"query": "old search"},
        created_at=utc_now() - timedelta(hours=2),
        updated_at=utc_now() - timedelta(hours=2),
    )
    db.jobs.save(stale_job)
    app = create_fastapi_app(db)
    token = create_jwt_token({"tenant_id": tenant.tenant_id, "user_id": "user-stale-job", "role": "member"})
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/jobs",
            json={"agent_name": "lead_generation", "payload": {"query": "fresh search", "limit": 1}},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["existing"] is False
    assert payload["job_id"] != stale_job.id
    saved_stale = db.jobs.get(tenant.tenant_id, stale_job.id)
    assert saved_stale.status == "cancelled"


def test_auth_forms_do_not_prefill_admin_credentials(monkeypatch) -> None:
    import dashboard

    fake_st = _FakeStreamlit()
    monkeypatch.setattr(dashboard, "st", fake_st)
    monkeypatch.setattr(dashboard, "api_request", lambda *args, **kwargs: _FakeResponse(payload={}))

    dashboard.require_login()

    fields = {label: kwargs for label, kwargs in fake_st.text_input_calls}

    assert fields["Tenant ID"].get("value", "") == ""
    assert fields["Email"].get("value", "") == ""
    assert fields["Password"].get("value", "") == ""
    assert fields["Workspace / Tenant ID"].get("value", "") == ""
    assert fields["Business Name"].get("value", "") == ""
    assert fields["Work Email"].get("value", "") == ""
    assert fields["Your Name"].get("value", "") == ""
    assert fields["Password"].get("type") == "password"


def test_sidebar_shows_admin_for_admin_role(monkeypatch) -> None:
    import dashboard

    fake_st = _FakeStreamlit()
    fake_st.session_state["auth"] = {"tenant_id": "mian755", "email": "admin@example.test", "role": "ADMIN"}
    monkeypatch.setattr(dashboard, "st", fake_st)
    monkeypatch.setattr(dashboard, "st_autorefresh", None)

    dashboard.render_sidebar_navigation()

    module_options = [options for label, options, _ in fake_st.sidebar.radio_calls if label == "Module"][0]
    assert "Admin Panel" in module_options


def test_sidebar_hides_admin_for_member_role(monkeypatch) -> None:
    import dashboard

    fake_st = _FakeStreamlit()
    fake_st.session_state["auth"] = {"tenant_id": "tenant-user", "email": "member@example.test", "role": "member"}
    monkeypatch.setattr(dashboard, "st", fake_st)
    monkeypatch.setattr(dashboard, "st_autorefresh", None)

    dashboard.render_sidebar_navigation()

    module_options = [options for label, options, _ in fake_st.sidebar.radio_calls if label == "Module"][0]
    assert "Admin Panel" not in module_options


def test_sidebar_uses_clean_production_navigation(monkeypatch) -> None:
    import dashboard

    fake_st = _FakeStreamlit()
    fake_st.session_state["auth"] = {"tenant_id": "tenant-user", "email": "member@example.test", "role": "member"}
    fake_st.session_state["sidebar_module"] = "Leads"
    monkeypatch.setattr(dashboard, "st", fake_st)
    monkeypatch.setattr(dashboard, "st_autorefresh", None)

    dashboard.render_sidebar_navigation()

    module_options = [options for label, options, _ in fake_st.sidebar.radio_calls if label == "Module"][0]
    assert module_options == ["Dashboard", "Generate Leads", "Leads", "Email CRM", "WhatsApp CRM", "Marketing Kit", "Billing", "Settings"]

    removed_items = {
        "AI Agency Kit",
        "Offer Matchmaker",
        "WhatsApp Sales Kit",
        "Followups",
        "CSV Export",
        "Generate from Lead",
        "Ad Copy",
        "Reels Script",
        "7-Day Content Calendar",
        "Mini Agency Mode",
    }
    assert removed_items.isdisjoint(module_options)

    marketing_st = _FakeStreamlit()
    marketing_st.session_state["auth"] = fake_st.session_state["auth"]
    marketing_st.session_state["sidebar_module"] = "Marketing Kit"
    monkeypatch.setattr(dashboard, "st", marketing_st)

    dashboard.render_sidebar_navigation()

    marketing_modules = [options for label, options, _ in marketing_st.sidebar.radio_calls if label == "Module"][0]
    assert "Marketing Kit" in marketing_modules
    assert removed_items.isdisjoint(marketing_modules)


def test_auth_role_supports_common_response_shapes(monkeypatch) -> None:
    import dashboard

    fake_st = _FakeStreamlit()
    monkeypatch.setattr(dashboard, "st", fake_st)

    for payload in (
        {"role": "ADMIN"},
        {"user": {"role": "admin"}},
        {"user_role": "admin"},
        {"is_admin": True},
    ):
        fake_st.session_state["auth"] = payload
        assert dashboard.is_admin_user() is True


def test_outreach_error_helper_maps_unknown_failure() -> None:
    import dashboard

    assert (
        dashboard.format_outreach_error("unknown_outreach_failure")
        == "This failed before detailed diagnostics were enabled. Re-run outreach to get the exact reason."
    )


def test_contact_readiness_helper_distinguishes_email_phone_and_no_contact() -> None:
    import dashboard

    assert dashboard.contact_readiness_label("lead@example.test", "") == "Email-ready"
    assert dashboard.contact_readiness_label("", "+923000000000") == "Phone-only"
    assert dashboard.contact_readiness_label("", "", "info@example.test") == "Likely email"
    assert dashboard.contact_readiness_label("", "") == "No contact"
    assert dashboard.contact_next_action("", "+923000000000") == "Generate WhatsApp Sales Kit"
