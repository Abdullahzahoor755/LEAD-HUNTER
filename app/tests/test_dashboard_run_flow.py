from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import httpx
import pytest


@dataclass
class _FakeResponse:
    payload: dict[str, Any]
    is_success: bool = True
    status_code: int = 200
    headers: dict[str, str] | None = None
    body: str = "{}"
    json_error: bool = False

    @property
    def text(self) -> str:
        return self.body

    def json(self) -> dict[str, Any]:
        if self.json_error:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
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
        self.query_params: dict[str, Any] = {}
        self.text_input_calls: list[tuple[str, dict[str, Any]]] = []
        self.warning_calls: list[tuple[Any, tuple[Any, ...], dict[str, Any]]] = []
        self.info_calls: list[tuple[Any, tuple[Any, ...], dict[str, Any]]] = []
        self.rerun_count = 0
        self.sidebar = _FakeSidebar(self)

    def markdown(self, *args: Any, **kwargs: Any) -> None:
        return None

    def columns(self, *args: Any, **kwargs: Any):
        spec = args[0] if args else 2
        count = spec if isinstance(spec, int) else len(spec)
        return tuple(_NullContext() for _ in range(count))

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

    def selectbox(self, label: str, options: list[Any], **kwargs: Any) -> Any:
        choices = list(options)
        return choices[0] if choices else None

    def multiselect(self, label: str, options: list[Any], **kwargs: Any) -> list[Any]:
        return []

    def expander(self, *args: Any, **kwargs: Any):
        return _NullContext()

    def text_area(self, *args: Any, **kwargs: Any) -> str:
        return str(kwargs.get("value", "") or "")

    def caption(self, *args: Any, **kwargs: Any) -> None:
        return None

    def progress(self, *args: Any, **kwargs: Any) -> None:
        return None

    def success(self, *args: Any, **kwargs: Any) -> None:
        return None

    def info(self, *args: Any, **kwargs: Any) -> None:
        self.info_calls.append((args[0] if args else "", args, kwargs))
        return None

    def warning(self, *args: Any, **kwargs: Any) -> None:
        self.warning_calls.append((args[0] if args else "", args, kwargs))
        return None

    def dataframe(self, *args: Any, **kwargs: Any) -> None:
        return None

    def cache_data(self, *args: Any, **kwargs: Any):
        def decorator(func):
            func.clear = lambda: None
            return func

        return decorator


class _FakeSidebar:
    def __init__(self, app: _FakeStreamlit) -> None:
        self.app = app
        self.radio_calls: list[tuple[str, list[str], dict[str, Any]]] = []
        self.button_calls: list[tuple[str, dict[str, Any]]] = []

    def markdown(self, *args: Any, **kwargs: Any) -> None:
        return None

    def button(self, *args: Any, **kwargs: Any) -> bool:
        self.button_calls.append((str(args[0] if args else ""), kwargs))
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


class _AsyncRepo:
    def __init__(self, repo: Any) -> None:
        self.repo = repo

    async def list(self, tenant_id: str):
        return self.repo.list(tenant_id)

    async def list_all(self):
        return self.repo.list_all()

    async def get(self, tenant_id: str, item_id: str):
        return self.repo.get(tenant_id, item_id)

    async def save(self, item: Any):
        return self.repo.save(item)

    async def delete(self, tenant_id: str, item_id: str):
        return self.repo.delete(tenant_id, item_id)

    async def find_by_company_url(self, tenant_id: str, company_url: str):
        return self.repo.find_by_company_url(tenant_id, company_url)

    async def find_by_email(self, tenant_id: str, email: str):
        return self.repo.find_by_email(tenant_id, email)

    async def claim_latest_matching_for_tenant(self, tenant_id: str, queue: str, worker_id: str, job_type: str):
        return self.repo.claim_latest_matching_for_tenant(tenant_id, queue, worker_id, job_type)


class _FakeAsyncSession:
    def __init__(self, *, fail_commit: bool = False) -> None:
        self.commits = 0
        self.fail_commit = fail_commit

    async def commit(self) -> None:
        self.commits += 1
        if self.fail_commit:
            raise RuntimeError("commit failed")


def _async_db_from_memory(memory_db: Any, session: _FakeAsyncSession):
    from app.db.session import AsyncDatabaseSession

    return AsyncDatabaseSession(
        session=session,
        tenants=_AsyncRepo(memory_db.tenants),
        users=_AsyncRepo(memory_db.users),
        campaigns=_AsyncRepo(memory_db.campaigns),
        leads=_AsyncRepo(memory_db.leads),
        emails=_AsyncRepo(memory_db.emails),
        replies=_AsyncRepo(memory_db.replies),
        voice_calls=_AsyncRepo(memory_db.voice_calls),
        followups=_AsyncRepo(memory_db.followups),
        agent_runs=_AsyncRepo(memory_db.agent_runs),
        jobs=_AsyncRepo(memory_db.jobs),
        payments=_AsyncRepo(memory_db.payments),
        gmail_credentials=_AsyncRepo(memory_db.gmail_credentials),
    )


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


def test_unreachable_lead_generation_job_status_does_not_crash(monkeypatch) -> None:
    import dashboard

    fake_st = _FakeStreamlit()
    fake_st.session_state["active_lead_generation_job_id"] = "job-unreachable"
    fake_st.session_state["lead_generation_busy"] = True
    monkeypatch.setattr(dashboard, "st", fake_st)

    def fake_api_request(method: str, path: str, **kwargs: Any) -> _FakeResponse:
        raise httpx.RequestError("transport unavailable")

    monkeypatch.setattr(dashboard, "api_request", fake_api_request)

    active = dashboard.render_active_lead_generation_job()

    assert active is False
    assert fake_st.session_state["active_lead_generation_job_id"] == ""
    assert fake_st.session_state["lead_generation_busy"] is False
    assert any(call[0] == "Could not refresh job status right now." for call in fake_st.warning_calls)
    assert any(call[0] == "No active jobs." for call in fake_st.info_calls)


def test_stale_lead_generation_job_is_not_active() -> None:
    import dashboard

    now = dashboard.datetime(2026, 6, 19, 12, 0, tzinfo=dashboard.timezone.utc)
    stale = {
        "job_type": "lead_generation",
        "status": "running",
        "updated_at": "2026-06-19T11:20:00+00:00",
    }
    recent = {
        "job_type": "lead_generation",
        "status": "queued",
        "updated_at": "2026-06-19T11:45:00+00:00",
    }
    completed = {
        "job_type": "lead_generation",
        "status": "completed",
        "updated_at": "2026-06-19T11:59:00+00:00",
    }

    assert dashboard.is_active_lead_generation_job(stale, now=now) is False
    assert dashboard.is_active_lead_generation_job(recent, now=now) is True
    assert dashboard.is_active_lead_generation_job(completed, now=now) is False


def test_valid_query_token_survives_refresh(monkeypatch) -> None:
    import dashboard

    token = "refresh-token"
    fake_st = _FakeStreamlit()
    fake_st.query_params["auth_token"] = token
    monkeypatch.setattr(dashboard, "st", fake_st)
    monkeypatch.setattr(dashboard, "decode_jwt_token", lambda value: {"tenant_id": "tenant-refresh", "user_id": "user-refresh"} if value == token else {})

    def fake_api_request(method: str, path: str, **kwargs: Any) -> _FakeResponse:
        assert path == "/dashboard/snapshot"
        return _FakeResponse(payload={"tenant_id": "tenant-refresh", "lead_count": 0})

    monkeypatch.setattr(dashboard, "api_request", fake_api_request)

    dashboard.hydrate_auth_from_query()
    assert dashboard.validate_current_auth() is True

    auth = fake_st.session_state["auth"]
    assert auth["token"] == token
    assert auth["tenant_id"] == "tenant-refresh"
    assert fake_st.session_state["auth_validated_token"] == token


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
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
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


@pytest.mark.anyio
async def test_old_running_lead_generation_job_is_not_cancelled() -> None:
    import httpx

    from app.api.app import create_fastapi_app
    from app.core.auth import create_jwt_token
    from app.core.models import Job, Tenant, utc_now
    from app.db.session import build_memory_session

    db = build_memory_session()
    tenant = Tenant(tenant_id="tenant-running-job", subscription_plan="Free")
    db.tenants.save(tenant)
    running_job = Job(
        tenant_id=tenant.tenant_id,
        name="lead_generation",
        status="running",
        payload={"query": "old active search"},
        created_at=utc_now() - timedelta(hours=2),
        started_at=utc_now() - timedelta(minutes=5),
        locked_at=utc_now() - timedelta(minutes=5),
        locked_by="worker-active",
    )
    db.jobs.save(running_job)
    app = create_fastapi_app(db)
    token = create_jwt_token({"tenant_id": tenant.tenant_id, "user_id": "user-running-job", "role": "member"})
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/jobs",
            json={"agent_name": "lead_generation", "payload": {"query": "fresh search", "limit": 1}},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["existing"] is True
    assert payload["job_id"] == running_job.id
    saved_running = db.jobs.get(tenant.tenant_id, running_job.id)
    assert saved_running.status == "running"


@pytest.mark.anyio
async def test_jobs_endpoint_commits_before_background_worker_runs() -> None:
    import httpx

    from app.api.app import create_fastapi_app
    from app.core.auth import create_jwt_token
    from app.core.models import Tenant
    from app.db.session import build_memory_session

    class FakeQueue:
        def __init__(self, session: _FakeAsyncSession) -> None:
            self.session = session
            self.registered: list[str] = []
            self.commit_count_seen_by_worker = -1

        async def register(self, job_id: str) -> None:
            self.registered.append(job_id)

        async def run_once_for_tenant(self, tenant, job_type: str = ""):
            self.commit_count_seen_by_worker = self.session.commits
            return {"status": "empty", "tenant_id": tenant.tenant_id}

    memory_db = build_memory_session()
    tenant = Tenant(tenant_id="tenant-commit-before-worker", subscription_plan="Free")
    memory_db.tenants.save(tenant)
    fake_session = _FakeAsyncSession()
    async_db = _async_db_from_memory(memory_db, fake_session)
    app = create_fastapi_app(async_db)
    fake_queue = FakeQueue(fake_session)
    app.state.queue = fake_queue
    token = create_jwt_token({"tenant_id": tenant.tenant_id, "user_id": "user-commit-worker", "role": "member"})
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/jobs",
            json={"agent_name": "lead_generation", "payload": {"query": "IT companies in Riyadh", "limit": 2}},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["existing"] is False
    assert fake_queue.registered == [body["job_id"]]
    assert fake_session.commits == 1
    assert fake_queue.commit_count_seen_by_worker == 1


@pytest.mark.anyio
async def test_jobs_endpoint_does_not_schedule_worker_when_commit_fails() -> None:
    import httpx

    from app.api.app import create_fastapi_app
    from app.core.auth import create_jwt_token
    from app.core.models import Tenant
    from app.db.session import build_memory_session

    class FakeQueue:
        def __init__(self) -> None:
            self.registered: list[str] = []
            self.runs = 0

        async def register(self, job_id: str) -> None:
            self.registered.append(job_id)

        async def run_once_for_tenant(self, tenant, job_type: str = ""):
            self.runs += 1
            return {"status": "empty", "tenant_id": tenant.tenant_id}

    memory_db = build_memory_session()
    tenant = Tenant(tenant_id="tenant-commit-fails", subscription_plan="Free")
    memory_db.tenants.save(tenant)
    async_db = _async_db_from_memory(memory_db, _FakeAsyncSession(fail_commit=True))
    app = create_fastapi_app(async_db)
    fake_queue = FakeQueue()
    app.state.queue = fake_queue
    token = create_jwt_token({"tenant_id": tenant.tenant_id, "user_id": "user-commit-fails", "role": "member"})
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/jobs",
            json={"agent_name": "lead_generation", "payload": {"query": "IT companies in Riyadh", "limit": 2}},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 500
    assert fake_queue.registered
    assert fake_queue.runs == 0


@pytest.mark.anyio
async def test_ui_created_lead_generation_job_completes_and_links_saved_leads() -> None:
    import httpx

    from app.agents.base import AgentRequest, BaseAgent
    from app.agents.registry import AgentRegistry
    from app.api.app import create_fastapi_app
    from app.core.auth import create_jwt_token
    from app.core.models import Lead, Tenant, TenantContext
    from app.db.session import build_memory_session
    from app.workers.jobs import AsyncJobQueue

    class SavingLeadAgent(BaseAgent):
        name = "lead_generation"

        async def run(self, request: AgentRequest, db) -> dict[str, Any]:
            job_id = str(request.payload.get("_job_id", ""))
            db.for_tenant(request.tenant).save(
                "leads",
                Lead(
                    tenant_id=request.tenant.tenant_id,
                    job_id=job_id,
                    company="Riyadh IT Co",
                    company_url="https://riyadh-it.test",
                    verified_email="hello@riyadh-it.test",
                    source_query=str(request.payload.get("query", "")),
                ),
            )
            return {"status": "SUCCESS", "message": "saved", "data": {"saved_leads": 1, "lead_count": 1}}

    db = build_memory_session()
    tenant = Tenant(tenant_id="tenant-ui-job-completes", subscription_plan="Free")
    db.tenants.save(tenant)
    app = create_fastapi_app(db)
    registry = AgentRegistry()
    registry.register(SavingLeadAgent())
    app.state.queue = AsyncJobQueue(db=db, agents=registry)
    token = create_jwt_token({"tenant_id": tenant.tenant_id, "user_id": "user-ui-job", "role": "member"})
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/jobs",
            json={"agent_name": "lead_generation", "payload": {"query": "IT companies in Riyadh", "limit": 2}},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    job_id = response.json()["job_id"]
    job = db.for_tenant(TenantContext(tenant_id=tenant.tenant_id)).get("jobs", job_id)
    leads = db.for_tenant(TenantContext(tenant_id=tenant.tenant_id)).list("leads")
    assert job.status == "completed"
    assert job.started_at is not None
    assert job.completed_at is not None
    assert len(leads) == 1
    assert leads[0].job_id == job_id


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

    module_options = [label.replace("● ", "") for label, kwargs in fake_st.sidebar.button_calls if str(kwargs.get("key", "")).startswith("nav_")]
    assert "Admin Panel" in module_options


def test_sidebar_shows_admin_for_top_level_session_role(monkeypatch) -> None:
    import dashboard

    fake_st = _FakeStreamlit()
    fake_st.session_state["auth"] = {"tenant_id": "mian755", "email": ""}
    fake_st.session_state["user_role"] = "admin"
    monkeypatch.setattr(dashboard, "st", fake_st)
    monkeypatch.setattr(dashboard, "st_autorefresh", None)

    dashboard.render_sidebar_navigation()

    module_options = [label.replace("● ", "") for label, kwargs in fake_st.sidebar.button_calls if str(kwargs.get("key", "")).startswith("nav_")]
    assert "Admin Panel" in module_options


def test_sidebar_hides_admin_for_member_role(monkeypatch) -> None:
    import dashboard

    fake_st = _FakeStreamlit()
    fake_st.session_state["auth"] = {"tenant_id": "tenant-user", "email": "member@example.test", "role": "member"}
    monkeypatch.setattr(dashboard, "st", fake_st)
    monkeypatch.setattr(dashboard, "st_autorefresh", None)

    dashboard.render_sidebar_navigation()

    module_options = [label.replace("● ", "") for label, kwargs in fake_st.sidebar.button_calls if str(kwargs.get("key", "")).startswith("nav_")]
    assert "Admin Panel" not in module_options


def test_sidebar_uses_clean_production_navigation(monkeypatch) -> None:
    import dashboard

    fake_st = _FakeStreamlit()
    fake_st.session_state["auth"] = {"tenant_id": "tenant-user", "email": "member@example.test", "role": "member"}
    fake_st.session_state["sidebar_module"] = "Leads"
    monkeypatch.setattr(dashboard, "st", fake_st)
    monkeypatch.setattr(dashboard, "st_autorefresh", None)

    dashboard.render_sidebar_navigation()

    module_options = [label.replace("● ", "") for label, kwargs in fake_st.sidebar.button_calls if str(kwargs.get("key", "")).startswith("nav_")]
    assert module_options == ["Dashboard", "Generate Leads", "Leads", "Email CRM", "WhatsApp CRM", "Marketing Kit", "Voice Calls"]

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

    marketing_modules = [label.replace("● ", "") for label, kwargs in marketing_st.sidebar.button_calls if str(kwargs.get("key", "")).startswith("nav_")]
    assert "Marketing Kit" in marketing_modules
    assert "Voice Calls" in marketing_modules
    assert removed_items.isdisjoint(marketing_modules)


def test_sidebar_account_menu_contains_billing_settings_and_logout(monkeypatch) -> None:
    import dashboard

    fake_st = _FakeStreamlit()
    fake_st.session_state["auth"] = {"tenant_id": "tenant-user", "email": "member@example.test", "role": "member"}
    fake_st.session_state["account_menu_open"] = True
    monkeypatch.setattr(dashboard, "st", fake_st)
    monkeypatch.setattr(dashboard, "st_autorefresh", None)

    dashboard.render_sidebar_navigation()

    main_modules = [label.replace("● ", "") for label, kwargs in fake_st.sidebar.button_calls if str(kwargs.get("key", "")).startswith("nav_")]
    account_items = [label.replace("● ", "") for label, kwargs in fake_st.sidebar.button_calls if str(kwargs.get("key", "")).startswith("account_nav_")]
    all_labels = [label for label, _ in fake_st.sidebar.button_calls]

    assert "Billing" not in main_modules
    assert "Settings" not in main_modules
    assert account_items == ["Billing", "Settings"]
    assert "Logout" in all_labels


def test_render_voice_calls_page_exists_and_handles_api_failure(monkeypatch) -> None:
    import dashboard
    from app.core.models import TenantContext

    fake_st = _FakeStreamlit()
    monkeypatch.setattr(dashboard, "st", fake_st)

    def fake_api_request(method: str, path: str, **kwargs: Any) -> _FakeResponse:
        return _FakeResponse(payload={"detail": "unavailable"}, is_success=False, status_code=503)

    monkeypatch.setattr(dashboard, "api_request", fake_api_request)
    monkeypatch.setattr(dashboard, "load_dashboard_data", lambda tenant: (_ for _ in ()).throw(RuntimeError("leads down")))
    dashboard.load_voice_calls.clear()

    assert hasattr(dashboard, "render_voice_calls_page")
    dashboard.render_voice_calls_page(TenantContext(tenant_id="tenant-ui", user_id="user-ui"))

    warnings = [str(item[0]) for item in fake_st.warning_calls]
    assert any("Voice provider status unavailable" in item for item in warnings)
    assert any("Voice Calls API is not available right now." in item for item in warnings)


def test_voice_calls_page_handles_non_json_response_safely(monkeypatch) -> None:
    import dashboard
    from app.core.models import TenantContext

    fake_st = _FakeStreamlit()
    monkeypatch.setattr(dashboard, "st", fake_st)

    def fake_api_request(method: str, path: str, **kwargs: Any) -> _FakeResponse:
        if path.startswith("/voice/calls"):
            return _FakeResponse(
                payload={},
                is_success=False,
                status_code=502,
                headers={"content-type": "text/html"},
                body="<html>backend exploded</html>",
                json_error=True,
            )
        return _FakeResponse(payload={"configured": False, "provider_reachable": False}, headers={"content-type": "application/json"})

    monkeypatch.setattr(dashboard, "api_request", fake_api_request)
    monkeypatch.setattr(dashboard, "load_dashboard_data", lambda tenant: (_ for _ in ()).throw(RuntimeError("leads down")))
    dashboard.load_voice_calls.clear()

    dashboard.render_voice_calls_page(TenantContext(tenant_id="tenant-ui", user_id="user-ui"))

    warnings = [str(item[0]) for item in fake_st.warning_calls]
    assert any("Voice Calls API is not available right now." in item for item in warnings)
    assert all("Expecting value" not in item for item in warnings)
    assert all("<html>" not in item for item in warnings)


def test_voice_calls_page_handles_backend_500_safely(monkeypatch) -> None:
    import dashboard
    from app.core.models import TenantContext

    fake_st = _FakeStreamlit()
    monkeypatch.setattr(dashboard, "st", fake_st)

    def fake_api_request(method: str, path: str, **kwargs: Any) -> _FakeResponse:
        if path.startswith("/voice/calls"):
            return _FakeResponse(
                payload={"detail": "Voice calls could not be loaded right now."},
                is_success=False,
                status_code=500,
                headers={"content-type": "application/json"},
            )
        return _FakeResponse(payload={"configured": False, "provider_reachable": False}, headers={"content-type": "application/json"})

    monkeypatch.setattr(dashboard, "api_request", fake_api_request)
    monkeypatch.setattr(dashboard, "load_dashboard_data", lambda tenant: (_ for _ in ()).throw(RuntimeError("leads down")))
    dashboard.load_voice_calls.clear()

    dashboard.render_voice_calls_page(TenantContext(tenant_id="tenant-ui", user_id="user-ui"))

    warnings = [str(item[0]) for item in fake_st.warning_calls]
    assert any("Voice calls could not be loaded right now." in item for item in warnings)
    assert all("Expecting value" not in item for item in warnings)


def test_voice_call_detail_handles_non_json_response_safely(monkeypatch) -> None:
    import dashboard
    from app.core.models import TenantContext

    fake_st = _FakeStreamlit()
    monkeypatch.setattr(dashboard, "st", fake_st)
    monkeypatch.setattr(
        dashboard,
        "load_voice_calls",
        lambda tenant, limit=50: [
            {
                "id": "call-1",
                "lead_id": "lead-1",
                "status": "completed",
                "outcome": "unknown",
                "summary": "",
                "duration_seconds": 0,
                "called_at": "",
                "created_at": "2026-01-01T00:00:00",
            }
        ],
    )
    monkeypatch.setattr(dashboard, "load_dashboard_data", lambda tenant: (_ for _ in ()).throw(RuntimeError("leads down")))

    def fake_api_request(method: str, path: str, **kwargs: Any) -> _FakeResponse:
        if path == "/voice/calls/call-1":
            return _FakeResponse(
                payload={},
                is_success=False,
                status_code=502,
                headers={"content-type": "text/html"},
                body="<html>bad gateway</html>",
                json_error=True,
            )
        return _FakeResponse(payload={"configured": False, "provider_reachable": False}, headers={"content-type": "application/json"})

    monkeypatch.setattr(dashboard, "api_request", fake_api_request)

    dashboard.render_voice_calls_page(TenantContext(tenant_id="tenant-ui", user_id="user-ui"))

    warnings = [str(item[0]) for item in fake_st.warning_calls]
    assert any("Could not load selected call: Voice Calls API is not available right now." in item for item in warnings)
    assert all("Expecting value" not in item for item in warnings)
    assert all("<html>" not in item for item in warnings)


def test_auth_role_supports_common_response_shapes(monkeypatch) -> None:
    import dashboard

    fake_st = _FakeStreamlit()
    monkeypatch.setattr(dashboard, "st", fake_st)

    for payload in (
        {"role": "ADMIN"},
        {"user": {"role": "admin"}},
        {"user_role": "admin"},
        {"is_admin": True},
        {"admin": True},
    ):
        fake_st.session_state["auth"] = payload
        assert dashboard.is_admin_user() is True

    fake_st.session_state["auth"] = {}
    fake_st.session_state["role"] = "admin"
    assert dashboard.is_admin_user() is True

    fake_st.session_state["role"] = ""
    fake_st.session_state["user_role"] = "admin"
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
