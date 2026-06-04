"""Tenant-safe Gmail reply monitor agent."""

from __future__ import annotations

from typing import Any, Dict

import leads as legacy_leads

from app.agents.base import AgentRequest, BaseAgent
from app.core.models import AgentRun
from app.db.session import AsyncDatabaseSession, DatabaseSession
from app.providers.base import ProviderAccount
from app.providers.registry import build_provider_registry
from app.services.agent_run_service import AgentRunService
from app.services.outreach_service import OutreachService
from app.services.plan_gate import require_pro_plan
from app.services.provider_credential_service import ProviderCredentialService
from app.services.reply_ai_service import ReplyAiService


class ReplyMonitorAgent(BaseAgent):
    name = "reply_monitor"

    async def run(self, request: AgentRequest, db: DatabaseSession | AsyncDatabaseSession) -> Dict[str, object]:
        await require_pro_plan(db, request.tenant)
        credential_service = ProviderCredentialService(db)
        service = OutreachService(db)
        ai_service = ReplyAiService()
        provider = build_provider_registry()["gmail"]
        credentials = await credential_service.get_gmail_credentials(request.tenant)
        if not credentials:
            raise ValueError("Tenant Gmail credentials are not configured.")
        account = ProviderAccount(tenant_id=request.tenant.tenant_id, **credentials)
        processed = 0
        my_email = str(credentials.get("email_address", "")).strip().lower()
        for item in await service.list_reply_candidates(request.tenant):
            lead = item["lead"]
            thread_id = item["thread_id"]
            replies = await provider.fetch_replies(account, thread_id)
            if not replies:
                continue
            inbound = []
            for reply in replies:
                sender_email = legacy_leads.extract_email_address(reply.from_address)
                if my_email and sender_email == my_email:
                    continue
                inbound.append(reply)
            if not inbound:
                continue
            inbound.sort(key=lambda reply: int(str(reply.metadata.get("internal_date", "0")) or "0"))
            latest_reply = inbound[-1]
            last_reply_at = legacy_leads.parse_gmail_internal_date(dict(latest_reply.metadata.get("raw", {})))
            if last_reply_at and last_reply_at == str((lead.metadata or {}).get("LastReplyAt", "")):
                continue
            analysis = await ai_service.classify(
                company=lead.company,
                sender_email=legacy_leads.extract_email_address(latest_reply.from_address),
                subject=latest_reply.subject,
                reply_text=latest_reply.body,
            )
            updates: Dict[str, Any] = {
                "ReplyStatus": "Received",
                "ReplyClassification": str(analysis.get("classification", "")),
                "Sentiment": str(analysis.get("sentiment", "")),
                "LeadTemperature": str(analysis.get("lead_temperature", "")),
                "LastReply": str(analysis.get("reason", "")),
                "LastReplyAt": last_reply_at,
                "LastReplyFrom": legacy_leads.extract_email_address(latest_reply.from_address),
                "LastReplySnippet": latest_reply.body[:500],
                "MeetingRequested": "Yes" if str(analysis.get("classification", "")) == "Interested" else str((lead.metadata or {}).get("MeetingRequested", "No")),
                "NextFollowupDue": "",
                "ReplyConfidenceScore": str(analysis.get("confidence_score", "")),
                "NextActionSuggestion": str(analysis.get("next_action_suggestion", "")),
            }
            await service.save_reply_result(
                tenant=request.tenant,
                lead=lead,
                email_id=getattr(item["last_email"], "id", ""),
                message=dict(latest_reply.metadata.get("raw", {})),
                updates=updates,
                received_at=legacy_leads.parse_datetime(last_reply_at),
            )
            processed += 1
        run = AgentRun(
            tenant_id=request.tenant.tenant_id,
            agent_name=self.name,
            status="completed",
            input_payload=request.payload,
            output_payload={"checked": processed},
        )
        await AgentRunService(db).record_run(request.tenant, run)
        return run.output_payload
