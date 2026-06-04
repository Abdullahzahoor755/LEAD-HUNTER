"""Outreach agent for tenant-aware email orchestration."""

from __future__ import annotations

from typing import Dict

import leads as legacy_leads

from app.agents.base import AgentRequest, BaseAgent
from app.core.models import AgentRun
from app.db.session import AsyncDatabaseSession, DatabaseSession
from app.providers.base import ProviderAccount, ProviderSendRequest
from app.providers.registry import build_provider_registry
from app.services.agent_run_service import AgentRunService
from app.services.outreach_service import OutreachService
from app.services.plan_gate import require_pro_plan
from app.services.provider_credential_service import ProviderCredentialService


class OutreachAgent(BaseAgent):
    name = "outreach"

    async def run(self, request: AgentRequest, db: DatabaseSession | AsyncDatabaseSession) -> Dict[str, object]:
        await require_pro_plan(db, request.tenant)
        service = OutreachService(db)
        credential_service = ProviderCredentialService(db)
        credentials = await credential_service.get_gmail_credentials(request.tenant)
        if not credentials:
            raise ValueError("Tenant Gmail credentials are not configured.")
        provider = build_provider_registry()["gmail"]
        account = ProviderAccount(tenant_id=request.tenant.tenant_id, **credentials)
        blocked_domains = {
            item.strip().lower()
            for item in legacy_leads.os.getenv("EMAIL_OPTOUT_DOMAINS", "").split(",")
            if item.strip()
        }
        sent = 0
        failed = 0
        for lead in await service.list_pending_outreach_leads(request.tenant, blocked_domains):
            try:
                subject, body = legacy_leads.generate_cold_email(
                    {
                        "Company": lead.company,
                        "Website": lead.website,
                        "Reason": lead.reason,
                    }
                )
                body = legacy_leads.append_unsubscribe_footer(body)
                sent_at = legacy_leads.now_utc()
                provider_result = await provider.send(
                    account,
                    ProviderSendRequest(to=lead.email, subject=subject, body=body),
                )
                await service.mark_outreach_result(
                    tenant=request.tenant,
                    lead=lead,
                    subject=subject,
                    body=body,
                    message_id=provider_result.message_id,
                    thread_id=provider_result.thread_id,
                    status="sent",
                    sent_at_iso=legacy_leads.to_iso8601(sent_at),
                    sent_at_dt=sent_at,
                )
                sent += 1
            except Exception:
                failed += 1
                await service.mark_outreach_result(
                    tenant=request.tenant,
                    lead=lead,
                    subject="",
                    body="",
                    message_id="",
                    thread_id="",
                    status="failed",
                    sent_at_iso=legacy_leads.to_iso8601(legacy_leads.now_utc()),
                    sent_at_dt=legacy_leads.now_utc(),
                )
        result = {"sent_messages": sent, "failed_messages": failed}
        run = AgentRun(
            tenant_id=request.tenant.tenant_id,
            agent_name=self.name,
            status="completed",
            input_payload=request.payload,
            output_payload=result,
        )
        await AgentRunService(db).record_run(request.tenant, run)
        return {"agent_run_id": run.id, **result}
