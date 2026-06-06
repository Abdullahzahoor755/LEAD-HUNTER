from __future__ import annotations

import httpx
import pytest

import app.api.app as api_module
from app.configs.settings import settings
from app.api.app import create_fastapi_app
from app.core.auth import decode_jwt_token
from app.core.models import Email, Job, Lead, Reply, TenantContext
from app.db.session import build_memory_session
from app.services.auth_service import AuthService
from scripts.fix_admin_roles import fix_admin_roles


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.anyio
async def test_admin_analytics_endpoints_require_admin_role() -> None:
    db = build_memory_session()
    admin = await AuthService(db).signup(
        tenant_id="tenant-admin",
        tenant_name="Tenant Admin",
        tenant_slug="tenant-admin",
        email="admin@test.local",
        password="secret123",
        full_name="Admin",
        plan="Agency",
        role="admin",
    )
    owner = await AuthService(db).signup(
        tenant_id="tenant-owner",
        tenant_name="Tenant Owner",
        tenant_slug="tenant-owner",
        email="owner@test.local",
        password="secret123",
        full_name="Owner",
        plan="Free",
    )
    owner_tenant = TenantContext(tenant_id="tenant-owner")
    db.for_tenant(owner_tenant).save(
        "leads",
        Lead(tenant_id="tenant-owner", company_url="https://owner.test", verified_email="lead@owner.test", industry="Technology"),
    )
    db.for_tenant(owner_tenant).save("jobs", Job(tenant_id="tenant-owner", name="lead_generation", status="queued"))
    db.for_tenant(owner_tenant).save("emails", Email(tenant_id="tenant-owner", status="sent", direction="outbound"))
    db.for_tenant(owner_tenant).save("replies", Reply(tenant_id="tenant-owner", from_email="lead@owner.test"))

    app = create_fastapi_app(db=db)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        forbidden = await client.get("/admin/summary", headers=_auth_headers(owner.token))
        summary = await client.get("/admin/summary", headers=_auth_headers(admin.token))
        users = await client.get("/admin/users", headers=_auth_headers(admin.token))
        usage = await client.get("/admin/tenants/usage", headers=_auth_headers(admin.token))
        leads = await client.get("/admin/leads/recent", headers=_auth_headers(admin.token))
        jobs = await client.get("/admin/jobs/recent", headers=_auth_headers(admin.token))

    assert forbidden.status_code == 403
    assert summary.status_code == 200
    assert summary.json()["total_users"] == 2
    assert summary.json()["total_tenants"] == 2
    assert summary.json()["total_leads"] == 1
    assert summary.json()["queued_jobs"] == 1
    assert users.status_code == 200
    assert usage.status_code == 200
    assert leads.status_code == 200
    assert jobs.status_code == 200
    assert "password_hash" not in users.text


@pytest.mark.anyio
async def test_admin_login_response_includes_role_and_plan() -> None:
    db = build_memory_session()
    await AuthService(db).signup(
        tenant_id="tenant-admin-login",
        tenant_name="Tenant Admin Login",
        tenant_slug="tenant-admin-login",
        email="admin-login@test.local",
        password="secret123",
        full_name="Admin Login",
        plan="Agency",
        role="admin",
    )

    app = create_fastapi_app(db=db)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/login",
            json={
                "tenant_id": "tenant-admin-login",
                "email": "admin-login@test.local",
                "password": "secret123",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["token"]
    assert body["tenant_id"] == "tenant-admin-login"
    assert body["email"] == "admin-login@test.local"
    assert body["role"] == "admin"
    assert body["plan"] == "Agency"
    assert body["subscription_plan"] == "Agency"
    assert decode_jwt_token(body["token"])["role"] == "admin"
    assert "password" not in body
    assert "password_hash" not in body


@pytest.mark.anyio
async def test_fix_admin_roles_keeps_only_selected_admin() -> None:
    db = build_memory_session()
    await AuthService(db).signup(
        tenant_id="mian755",
        tenant_name="Main Admin",
        tenant_slug="mian755",
        email="main@mian755.test",
        password="secret123",
        full_name="Main Admin",
        plan="Agency",
        role="admin",
    )
    await AuthService(db).signup(
        tenant_id="tenant-other-admin",
        tenant_name="Other Admin",
        tenant_slug="tenant-other-admin",
        email="other-admin@test.local",
        password="secret123",
        full_name="Other Admin",
        plan="Agency",
        role="admin",
    )
    await AuthService(db).signup(
        tenant_id="tenant-normal",
        tenant_name="Normal",
        tenant_slug="tenant-normal",
        email="normal@test.local",
        password="secret123",
        full_name="Normal",
        plan="Free",
    )

    dry_run = await fix_admin_roles(db, admin_identifier="mian755", apply=False)
    other_before = db.users.find_by_email("tenant-other-admin", "other-admin@test.local")
    applied = await fix_admin_roles(db, admin_identifier="mian755", apply=True)
    main = db.users.find_by_email("mian755", "main@mian755.test")
    other = db.users.find_by_email("tenant-other-admin", "other-admin@test.local")
    normal = db.users.find_by_email("tenant-normal", "normal@test.local")

    assert dry_run.demoted_count == 0
    assert other_before.role == "admin"
    assert applied.demoted_count == 1
    assert main.role == "admin"
    assert other.role == "member"
    assert other.status == "active"
    assert other.metadata["previous_admin_role"] == "admin"
    assert normal.role == "owner"


@pytest.mark.anyio
async def test_recent_jobs_endpoint_is_tenant_scoped() -> None:
    db = build_memory_session()
    app = create_fastapi_app(db=db)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        first = (
            await client.post(
                "/signup",
                json={
                    "tenant_id": "tenant-jobs-a",
                    "tenant_name": "Tenant Jobs A",
                    "tenant_slug": "tenant-jobs-a",
                    "email": "owner@tenant-jobs-a.test",
                    "password": "secret123",
                    "full_name": "Owner A",
                },
            )
        ).json()
        second = (
            await client.post(
                "/signup",
                json={
                    "tenant_id": "tenant-jobs-b",
                    "tenant_name": "Tenant Jobs B",
                    "tenant_slug": "tenant-jobs-b",
                    "email": "owner@tenant-jobs-b.test",
                    "password": "secret123",
                    "full_name": "Owner B",
                },
            )
        ).json()
        await client.post("/jobs", headers=_auth_headers(first["token"]), json={"agent_name": "lead_generation", "payload": {}})
        await client.post("/jobs", headers=_auth_headers(second["token"]), json={"agent_name": "lead_generation", "payload": {}})
        jobs = await client.get("/jobs/recent", headers=_auth_headers(first["token"]))

    assert jobs.status_code == 200
    body = jobs.json()
    assert body["tenant_id"] == "tenant-jobs-a"
    assert {item["tenant_id"] for item in body["items"]} == {"tenant-jobs-a"}


@pytest.mark.anyio
async def test_signup_login_and_auth_middleware() -> None:
    app = create_fastapi_app(db=build_memory_session())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        signup = await client.post(
            "/signup",
            json={
                "tenant_id": "tenant-a",
                "tenant_name": "Tenant A",
                "tenant_slug": "tenant-a",
                "email": "owner@tenant-a.test",
                "password": "secret123",
                "full_name": "Tenant A Owner",
                "plan": "Starter",
            },
        )
        assert signup.status_code == 200

        login = await client.post(
            "/login",
            json={"tenant_id": "tenant-a", "email": "owner@tenant-a.test", "password": "secret123"},
        )
        assert login.status_code == 200
        token = login.json()["token"]
        payload = decode_jwt_token(token)
        assert payload["tenant_id"] == "tenant-a"

        leads = await client.get("/leads", headers=_auth_headers(token))
        assert leads.status_code == 200
        assert leads.json()["tenant_id"] == "tenant-a"


@pytest.mark.anyio
async def test_tenant_isolation_and_lead_upsert() -> None:
    app = create_fastapi_app(db=build_memory_session())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        first = (
            await client.post(
                "/signup",
                json={
                    "tenant_id": "tenant-one",
                    "tenant_name": "Tenant One",
                    "tenant_slug": "tenant-one",
                    "email": "owner@tenant-one.test",
                    "password": "secret123",
                    "full_name": "Owner One",
                },
            )
        ).json()
        second = (
            await client.post(
                "/signup",
                json={
                    "tenant_id": "tenant-two",
                    "tenant_name": "Tenant Two",
                    "tenant_slug": "tenant-two",
                    "email": "owner@tenant-two.test",
                    "password": "secret123",
                    "full_name": "Owner Two",
                },
            )
        ).json()

        headers_one = _auth_headers(first["token"])
        headers_two = _auth_headers(second["token"])

        create = await client.post(
            "/leads",
            headers=headers_one,
            json={"company": "Acme", "email": "lead@acme.test", "website": "https://acme.test", "score": 50},
        )
        assert create.status_code == 200
        update = await client.post(
            "/leads",
            headers=headers_one,
            json={"company": "Acme Updated", "email": "lead@acme.test", "website": "https://acme.test", "score": 90},
        )
        assert update.status_code == 200
        assert update.json()["company"] == "Acme Updated"
        assert update.json()["score"] == 90

        tenant_one_leads = (await client.get("/leads", headers=headers_one)).json()["items"]
        tenant_two_leads = (await client.get("/leads", headers=headers_two)).json()["items"]
        assert len(tenant_one_leads) == 1
        assert tenant_two_leads == []


@pytest.mark.anyio
async def test_queue_run_and_dashboard_snapshot() -> None:
    app = create_fastapi_app(db=build_memory_session())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        auth = (
            await client.post(
                "/signup",
                json={
                    "tenant_id": "tenant-jobs",
                    "tenant_name": "Tenant Jobs",
                    "tenant_slug": "tenant-jobs",
                    "email": "owner@tenant-jobs.test",
                    "password": "secret123",
                    "full_name": "Owner Jobs",
                    "plan": "Pro",
                },
            )
        ).json()
        headers = _auth_headers(auth["token"])

        await client.post(
            "/leads",
            headers=headers,
            json={"company": "Queued Co", "email": "lead@queue.test", "website": "https://queue.test", "score": 70},
        )
        enqueue = await client.post("/jobs", headers=headers, json={"agent_name": "reply_monitor", "payload": {"mode": "once"}})
        assert enqueue.status_code == 200

        run = await client.post("/jobs/run-once", headers=headers)
        assert run.status_code == 200
        assert run.json()["status"] in {"completed", "failed"}

        snapshot = await client.get("/dashboard/snapshot", headers=headers)
        assert snapshot.status_code == 200
        body = snapshot.json()
        assert body["tenant_id"] == "tenant-jobs"
        assert body["lead_count"] == 1
        assert body["job_count"] >= 1


@pytest.mark.anyio
async def test_free_plan_allows_lead_generation_job_and_blocks_outreach_jobs() -> None:
    app = create_fastapi_app(db=build_memory_session())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        auth = (
            await client.post(
                "/signup",
                json={
                    "tenant_id": "tenant-free-gates",
                    "tenant_name": "Tenant Free Gates",
                    "tenant_slug": "tenant-free-gates",
                    "email": "owner@tenant-free-gates.test",
                    "password": "secret123",
                    "full_name": "Owner Free",
                    "plan": "Free",
                },
            )
        ).json()
        headers = _auth_headers(auth["token"])

        lead_generation = await client.post(
            "/jobs",
            headers=headers,
            json={"agent_name": "lead_generation", "payload": {"limit": 1, "query": "software companies"}},
        )
        outreach = await client.post("/jobs", headers=headers, json={"agent_name": "outreach", "payload": {}})
        reply_monitor = await client.post("/jobs", headers=headers, json={"agent_name": "reply_monitor", "payload": {}})
        followup = await client.post("/jobs", headers=headers, json={"agent_name": "followup", "payload": {}})

    assert lead_generation.status_code == 200
    assert outreach.status_code == 403
    assert outreach.json()["detail"] == "Outreach is available in Pro plan."
    assert reply_monitor.status_code == 403
    assert followup.status_code == 403


@pytest.mark.anyio
async def test_readyz_is_accessible_without_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_verify_async_database():
        return {"ok": True, "backend": "postgres"}

    monkeypatch.setattr(api_module, "verify_async_database", fake_verify_async_database)
    app = create_fastapi_app(db=build_memory_session())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/readyz")

    assert response.status_code == 200
    assert response.json()["ok"] is True


@pytest.mark.anyio
async def test_debug_lead_serialization_is_hidden_outside_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setattr(settings, "environment", "staging")
    app = create_fastapi_app(db=build_memory_session())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        auth = (
            await client.post(
                "/signup",
                json={
                    "tenant_id": "tenant-debug",
                    "tenant_name": "Tenant Debug",
                    "tenant_slug": "tenant-debug",
                    "email": "owner@tenant-debug.test",
                    "password": "secret123",
                    "full_name": "Owner Debug",
                },
            )
        ).json()
        response = await client.get("/debug/leads/serialization", headers=_auth_headers(auth["token"]))

    assert response.status_code == 404


def test_production_rejects_default_jwt_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "jwt_secret", "dev-secret-change-me")

    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        create_fastapi_app(db=build_memory_session())
