from __future__ import annotations

from contextlib import asynccontextmanager

import httpx
import pytest

import app.api.app as api_module
import app.workers.jobs as jobs_module
from app.api.app import create_fastapi_app
from app.core.tenant import assert_same_tenant
from app.db.session import build_memory_session


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class AsyncTenantScopeAdapter:
    def __init__(self, db, tenant) -> None:
        self.db = db
        self.tenant = tenant

    async def list(self, repository_name: str):
        return self.db.for_tenant(self.tenant).list(repository_name)

    async def get(self, repository_name: str, item_id: str):
        item = self.db.for_tenant(self.tenant).get(repository_name, item_id)
        if item is not None:
            assert_same_tenant(self.tenant.tenant_id, item.tenant_id)
        return item

    async def save(self, repository_name: str, item):
        assert_same_tenant(self.tenant.tenant_id, item.tenant_id)
        return self.db.for_tenant(self.tenant).save(repository_name, item)


class AsyncMemoryAdapter:
    def __init__(self, db) -> None:
        self._db = db
        self.tenants = AsyncRepositoryAdapter(db.tenants)
        self.users = AsyncRepositoryAdapter(db.users)
        self.campaigns = AsyncRepositoryAdapter(db.campaigns)
        self.leads = AsyncRepositoryAdapter(db.leads)
        self.emails = AsyncRepositoryAdapter(db.emails)
        self.replies = AsyncRepositoryAdapter(db.replies)
        self.followups = AsyncRepositoryAdapter(db.followups)
        self.agent_runs = AsyncRepositoryAdapter(db.agent_runs)
        self.jobs = AsyncRepositoryAdapter(db.jobs)

    def for_tenant(self, tenant):
        return AsyncTenantScopeAdapter(self._db, tenant)


class AsyncRepositoryAdapter:
    def __init__(self, repo) -> None:
        self.repo = repo

    async def list(self, tenant_id: str):
        return self.repo.list(tenant_id)

    async def get(self, tenant_id: str, item_id: str):
        return self.repo.get(tenant_id, item_id)

    async def save(self, item):
        return self.repo.save(item)

    async def delete(self, tenant_id: str, item_id: str):
        return self.repo.delete(tenant_id, item_id)

    async def find_by_email(self, tenant_id: str, email: str):
        return self.repo.find_by_email(tenant_id, email)

    async def find_by_company_url(self, tenant_id: str, company_url: str):
        return self.repo.find_by_company_url(tenant_id, company_url)

    async def next_queued(self, queue: str):
        return self.repo.next_queued(queue)

    async def claim_next_for_tenant(self, tenant_id: str, queue: str, worker_id: str):
        return self.repo.claim_next_for_tenant(tenant_id, queue, worker_id)

    async def get_any(self, item_id: str):
        return self.repo.get_any(item_id)


@pytest.mark.anyio
async def test_live_async_execution_path_persists_across_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    shared_db = AsyncMemoryAdapter(build_memory_session())

    @asynccontextmanager
    async def fake_async_db_session():
        yield shared_db

    async def fake_initialize_async_database() -> None:
        return None

    async def fake_verify_async_database():
        return {"ok": True, "backend": "postgres"}

    monkeypatch.setattr(api_module, "get_async_db_session", fake_async_db_session)
    monkeypatch.setattr(jobs_module, "get_async_db_session", fake_async_db_session)
    monkeypatch.setattr(api_module, "initialize_async_database", fake_initialize_async_database)
    monkeypatch.setattr(api_module, "verify_async_database", fake_verify_async_database)

    from app.configs.settings import settings

    original_backend = settings.database_backend
    original_legacy = settings.enable_legacy_runtime
    settings.database_backend = "postgres"
    settings.enable_legacy_runtime = False
    try:
        first_app = create_fastapi_app()
        first_transport = httpx.ASGITransport(app=first_app)
        async with httpx.AsyncClient(transport=first_transport, base_url="http://testserver") as client:
            signup = await client.post(
                "/signup",
                json={
                    "tenant_id": "tenant-live",
                    "tenant_name": "Tenant Live",
                    "tenant_slug": "tenant-live",
                    "email": "owner@tenant-live.test",
                    "password": "secret123",
                    "full_name": "Owner Live",
                    "plan": "Pro",
                },
            )
            assert signup.status_code == 200
            token = signup.json()["token"]
            headers = _auth_headers(token)

            lead = await client.post(
                "/leads",
                headers=headers,
                json={"company": "Persisted Co", "email": "lead@persisted.test", "website": "https://persisted.test", "score": 88},
            )
            assert lead.status_code == 200

            job = await client.post("/jobs", headers=headers, json={"agent_name": "reply_monitor", "payload": {"mode": "once"}})
            assert job.status_code == 200

        second_app = create_fastapi_app()
        second_transport = httpx.ASGITransport(app=second_app)
        async with httpx.AsyncClient(transport=second_transport, base_url="http://testserver") as client:
            login = await client.post(
                "/login",
                json={"tenant_id": "tenant-live", "email": "owner@tenant-live.test", "password": "secret123"},
            )
            assert login.status_code == 200
            token = login.json()["token"]
            headers = _auth_headers(token)

            leads = await client.get("/leads", headers=headers)
            assert leads.status_code == 200
            assert len(leads.json()["items"]) == 1
            assert leads.json()["items"][0]["company_url"] == "https://persisted.test"

            run = await client.post("/jobs/run-once", headers=headers)
            assert run.status_code == 200
            assert run.json()["status"] in {"completed", "failed"}

            snapshot = await client.get("/dashboard/snapshot", headers=headers)
            assert snapshot.status_code == 200
            body = snapshot.json()
            assert body["tenant_id"] == "tenant-live"
            assert body["lead_count"] == 1
            assert body["job_count"] >= 1

            other_signup = await client.post(
                "/signup",
                json={
                    "tenant_id": "tenant-other",
                    "tenant_name": "Tenant Other",
                    "tenant_slug": "tenant-other",
                    "email": "owner@tenant-other.test",
                    "password": "secret123",
                    "full_name": "Owner Other",
                },
            )
            other_headers = _auth_headers(other_signup.json()["token"])
            isolated = await client.get("/leads", headers=other_headers)
            assert isolated.status_code == 200
            assert isolated.json()["items"] == []
    finally:
        settings.database_backend = original_backend
        settings.enable_legacy_runtime = original_legacy
