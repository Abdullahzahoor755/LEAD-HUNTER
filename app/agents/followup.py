"""Tenant-safe follow-up worker agent."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict

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


class FollowupAgent(BaseAgent):
    name = "followup"

    async def run(self, request: AgentRequest, db: DatabaseSession | AsyncDatabaseSession) -> Dict[str, Any]:
        await require_pro_plan(db, request.tenant)
        service = OutreachService(db)
        credential_service = ProviderCredentialService(db)
        provider = build_provider_registry()["gmail"]
        credentials = await credential_service.get_gmail_credentials(request.tenant)
        if not credentials:
            raise ValueError("Tenant Gmail credentials are not configured.")
        account = ProviderAccount(tenant_id=request.tenant.tenant_id, **credentials)
        now_value = legacy_leads.now_utc()
        sent = 0
        blocked = {
            item.strip().lower()
            for item in legacy_leads.os.getenv("EMAIL_OPTOUT_DOMAINS", "").split(",")
            if item.strip()
        }
        candidates = await service.list_followup_candidates(request.tenant, blocked, now_value, legacy_leads.parse_datetime)
        for item in candidates:
            lead = item["lead"]
            last_email = item["last_email"]
            row = {
                "Company": lead.company,
                "Reason": lead.reason,
                "EmailSubject": getattr(last_email, "subject", ""),
                "LastEmailBody": getattr(last_email, "body", ""),
                "FollowupCount": str(int((lead.metadata or {}).get("FollowupCount", 0) or 0)),
            }
            followup_number = int(row["FollowupCount"] or "0") + 1
            subject, body = legacy_leads.generate_followup_email(row, followup_number)
            body = legacy_leads.append_unsubscribe_footer(body)
            thread_id = str((lead.metadata or {}).get("GmailThreadId", getattr(last_email, "provider_thread_id", "")))
            result = await provider.send(
                account,
                ProviderSendRequest(to=lead.email, subject=subject, body=body, thread_id=thread_id),
            )
            next_due = ""
            if followup_number < 3:
                next_due = legacy_leads.to_iso8601(now_value + timedelta(days=2))
            await service.save_followup_result(
                tenant=request.tenant,
                lead=lead,
                last_email_id=getattr(last_email, "id", ""),
                subject=subject,
                body=body,
                thread_id=result.thread_id or thread_id,
                followup_number=followup_number,
                now_iso=legacy_leads.to_iso8601(now_value),
                now_dt=now_value,
                next_due_iso=next_due,
            )
            sent += 1

        run = AgentRun(
            tenant_id=request.tenant.tenant_id,
            agent_name=self.name,
            status="completed",
            input_payload=request.payload,
            output_payload={"sent_followups": sent},
        )
        await AgentRunService(db).record_run(request.tenant, run)
        return {"agent_run_id": run.id, "sent_followups": sent}
