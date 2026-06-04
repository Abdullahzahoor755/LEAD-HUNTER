"""Async job orchestration service."""

from __future__ import annotations

from typing import Any, Dict

from app.core.models import Job, TenantContext
from app.db.session import AsyncDatabaseSession, DatabaseSession
from app.services._async import maybe_await


class JobService:
    def __init__(self, db: DatabaseSession | AsyncDatabaseSession) -> None:
        self.db = db

    async def enqueue(self, tenant: TenantContext, name: str, payload: Dict[str, Any], queue: str = "default") -> Job:
        max_attempts = int(payload.get("max_attempts", 3) or 3)
        normalized_payload = dict(payload)
        normalized_payload.pop("max_attempts", None)
        job = Job(tenant_id=tenant.tenant_id, name=name, queue=queue, payload=normalized_payload, max_attempts=max_attempts)
        return await maybe_await(self.db.for_tenant(tenant).save("jobs", job))

    async def get_job(self, tenant: TenantContext, job_id: str) -> Job | None:
        job = await maybe_await(self.db.for_tenant(tenant).get("jobs", job_id))
        return job
