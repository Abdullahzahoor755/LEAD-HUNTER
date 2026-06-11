"""Async background job system for agent execution."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import logging
from typing import Any, Dict
from uuid import uuid4

from app.agents.base import AgentRequest
from app.agents.registry import AgentRegistry
from app.core.auth import is_plan_gated_agent
from app.core.models import Job, TenantContext
from app.db.session import AsyncDatabaseSession, DatabaseSession, get_async_db_session
from app.services._async import maybe_await
from app.services.outreach_audit import audit_log
from app.services.plan_gate import require_pro_plan


LOGGER = logging.getLogger(__name__)


class AsyncJobQueue:
    def __init__(self, db: DatabaseSession | None, agents: AgentRegistry) -> None:
        self.db = db
        self.agents = agents
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.worker_id = f"worker-{uuid4().hex[:12]}"

    @asynccontextmanager
    async def session_scope(self):
        if self.db is not None:
            yield self.db
            return
        async with get_async_db_session() as db:
            yield db

    async def enqueue(self, job: Job) -> Job:
        async with self.session_scope() as db:
            await maybe_await(db.jobs.save(job))
        await self.queue.put(job.id)
        return job

    async def register(self, job_id: str) -> None:
        await self.queue.put(job_id)

    async def run_once(self) -> Dict[str, Any]:
        raise RuntimeError("Use run_once_for_tenant with an authenticated tenant context.")

    async def run_once_for_tenant(self, tenant: TenantContext | None = None, job_type: str = "") -> Dict[str, Any]:
        if tenant is not None:
            async with self.session_scope() as db:
                normalized_job_type = str(job_type or "").strip()
                if normalized_job_type and hasattr(db.jobs, "claim_latest_matching_for_tenant"):
                    job = await maybe_await(
                        db.jobs.claim_latest_matching_for_tenant(tenant.tenant_id, "default", self.worker_id, normalized_job_type)
                    )
                elif hasattr(db.jobs, "claim_next_for_tenant"):
                    job = await maybe_await(db.jobs.claim_next_for_tenant(tenant.tenant_id, "default", self.worker_id))
                elif hasattr(db.jobs, "next_queued_for_tenant"):
                    job = await maybe_await(db.jobs.next_queued_for_tenant(tenant.tenant_id, "default"))
                else:
                    job = None
                    for candidate in await maybe_await(db.jobs.list(tenant.tenant_id)):
                        if candidate.queue == "default" and candidate.status == "queued":
                            job = candidate
                            break
                if job is None:
                    audit_log(
                        LOGGER,
                        logging.INFO,
                        "OUTREACH_AUDIT job.empty tenant_id=%s requested_job_type=%s",
                        tenant.tenant_id,
                        normalized_job_type,
                    )
                    return {"status": "empty", "tenant_id": tenant.tenant_id}
                scoped_db = db.for_tenant(tenant)
                persisted_job = await maybe_await(scoped_db.get("jobs", job.id))
                if persisted_job is None:
                    audit_log(
                        LOGGER,
                        logging.WARNING,
                        "OUTREACH_AUDIT job.missing tenant_id=%s job_id=%s requested_job_type=%s",
                        tenant.tenant_id,
                        job.id,
                        normalized_job_type,
                    )
                    return {"status": "missing_job", "job_id": job.id, "tenant_id": tenant.tenant_id}
                audit_log(
                    LOGGER,
                    logging.INFO,
                    "OUTREACH_AUDIT job.claimed tenant_id=%s job_id=%s agent_name=%s requested_job_type=%s attempt_count=%s",
                    tenant.tenant_id,
                    persisted_job.id,
                    persisted_job.name,
                    normalized_job_type,
                    persisted_job.attempt_count,
                )
                try:
                    if is_plan_gated_agent(persisted_job.name):
                        await require_pro_plan(db, tenant)
                    agent = self.agents.get(persisted_job.name)
                    request_payload = dict(persisted_job.payload or {})
                    request_payload["_job_id"] = persisted_job.id
                    request = AgentRequest(tenant=tenant, payload=request_payload)
                    audit_log(
                        LOGGER,
                        logging.INFO,
                        "OUTREACH_AUDIT job.dispatch tenant_id=%s job_id=%s agent_name=%s payload=%s",
                        tenant.tenant_id,
                        persisted_job.id,
                        persisted_job.name,
                        persisted_job.payload,
                    )
                    LOGGER.info(
                        "Job stage start tenant_id=%s job_id=%s agent_name=%s stage=agent_run",
                        tenant.tenant_id,
                        persisted_job.id,
                        persisted_job.name,
                    )
                    result = await agent.run(request, db)
                    latest_job = await maybe_await(scoped_db.get("jobs", persisted_job.id))
                    if latest_job is not None:
                        persisted_job.result_summary = dict(latest_job.result_summary or {})
                    persisted_job.result = result
                    agent_status = str(result.get("status", "") if isinstance(result, dict) else "").strip().upper()
                    persisted_job.status = "failed" if agent_status == "FAILED" else "completed"
                    persisted_job.error = ""
                    if persisted_job.status == "failed":
                        persisted_job.error = str(result.get("message", "") if isinstance(result, dict) else "").strip()
                    persisted_job.locked_by = ""
                    persisted_job.locked_at = None
                    persisted_job.completed_at = datetime.now(timezone.utc)
                    audit_log(
                        LOGGER,
                        logging.INFO,
                        "OUTREACH_AUDIT job.completed tenant_id=%s job_id=%s agent_name=%s result=%s",
                        tenant.tenant_id,
                        persisted_job.id,
                        persisted_job.name,
                        result,
                    )
                except Exception as error:
                    should_retry = not isinstance(error, ValueError)
                    persisted_job.status = (
                        "queued"
                        if should_retry and int(persisted_job.attempt_count or 0) < int(persisted_job.max_attempts or 3)
                        else "failed"
                    )
                    persisted_job.error = str(error)
                    persisted_job.locked_by = ""
                    persisted_job.locked_at = None
                    persisted_job.completed_at = datetime.now(timezone.utc)
                    audit_log(
                        LOGGER,
                        logging.ERROR,
                        "OUTREACH_AUDIT job.failed tenant_id=%s job_id=%s agent_name=%s status=%s error=%s",
                        tenant.tenant_id,
                        persisted_job.id,
                        persisted_job.name,
                        persisted_job.status,
                        error,
                        exc_info=True,
                    )
                await maybe_await(scoped_db.save("jobs", persisted_job))
            return {
                "status": persisted_job.status,
                "job_id": persisted_job.id,
                "tenant_id": tenant.tenant_id,
                "result": persisted_job.result,
                "error": persisted_job.error,
                "attempt_count": persisted_job.attempt_count,
            }
