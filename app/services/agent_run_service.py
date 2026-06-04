"""Agent-run persistence kept behind a service boundary."""

from __future__ import annotations

from app.core.models import AgentRun, TenantContext
from app.db.session import AsyncDatabaseSession, DatabaseSession
from app.services._async import maybe_await


class AgentRunService:
    def __init__(self, db: DatabaseSession | AsyncDatabaseSession) -> None:
        self.db = db

    async def record_run(self, tenant: TenantContext, run: AgentRun) -> AgentRun:
        if run.tenant_id != tenant.tenant_id:
            raise ValueError("Agent run tenant mismatch.")
        return await maybe_await(self.db.for_tenant(tenant).save("agent_runs", run))
