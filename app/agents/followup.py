"""Tenant-safe follow-up worker agent."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import os
from typing import Any, Dict

from app.agents.base import AgentRequest, BaseAgent
from app.core.models import AgentRun
from app.db.session import AsyncDatabaseSession, DatabaseSession
from app.providers.base import ProviderAccount, ProviderSendRequest
from app.providers.registry import build_provider_registry
from app.services.agent_run_service import AgentRunService
from app.services.outreach_audit import audit_log
from app.services.outreach_email_service import OutreachEmailService
from app.services.outreach_service import OutreachService
from app.services.plan_gate import require_pro_plan
from app.services.provider_credential_service import ProviderCredentialService


LOGGER = logging.getLogger(__name__)


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
        audit_log(
            LOGGER,
            logging.INFO,
            "OUTREACH_AUDIT followup.credentials_loaded tenant_id=%s sender_email=%s has_refresh_token=%s has_access_token=%s",
            request.tenant.tenant_id,
            str(credentials.get("email_address", "") or ""),
            bool(credentials.get("refresh_token")),
            bool(credentials.get("access_token")),
        )
        account = ProviderAccount(tenant_id=request.tenant.tenant_id, **credentials)
        email_service = OutreachEmailService(db)
        now_value = datetime.now(timezone.utc)
        sent = 0
        blocked = {
            item.strip().lower()
            for item in os.getenv("EMAIL_OPTOUT_DOMAINS", "").split(",")
            if item.strip()
        }
        candidates = await service.list_followup_candidates(request.tenant, blocked, now_value, _parse_datetime)
        audit_log(
            LOGGER,
            logging.INFO,
            "OUTREACH_AUDIT followup.candidates tenant_id=%s candidate_count=%s blocked_domain_count=%s",
            request.tenant.tenant_id,
            len(candidates),
            len(blocked),
        )
        for item in candidates:
            lead = item["lead"]
            last_email = item["last_email"]
            audit_log(
                LOGGER,
                logging.INFO,
                "OUTREACH_AUDIT followup.lead_selected tenant_id=%s lead_id=%s email=%s last_email_id=%s",
                request.tenant.tenant_id,
                lead.id,
                lead.email,
                getattr(last_email, "id", ""),
            )
            followup_number = int((lead.metadata or {}).get("FollowupCount", 0) or 0) + 1
            generated = await email_service.generate_followup_email(
                tenant=request.tenant,
                lead=lead,
                followup_number=followup_number,
                previous_subject=str(getattr(last_email, "subject", "") or ""),
                previous_body=str(getattr(last_email, "body", "") or ""),
            )
            subject = str(generated.get("subject", "") or "").strip()
            body = email_service.ensure_unsubscribe_footer(str(generated.get("body", "") or "").strip())
            thread_id = str((lead.metadata or {}).get("GmailThreadId", getattr(last_email, "provider_thread_id", "")))
            audit_log(
                LOGGER,
                logging.INFO,
                "OUTREACH_AUDIT followup.email_generated tenant_id=%s lead_id=%s email=%s followup_number=%s subject=%r body_length=%s thread_id=%s mode=%s provider=%s",
                request.tenant.tenant_id,
                lead.id,
                lead.email,
                followup_number,
                subject,
                len(body),
                thread_id,
                str(generated.get("mode", "") or ""),
                str(generated.get("provider", "") or ""),
            )
            result = await provider.send(
                account,
                ProviderSendRequest(to=lead.email, subject=subject, body=body, thread_id=thread_id),
            )
            audit_log(
                LOGGER,
                logging.INFO,
                "OUTREACH_AUDIT followup.provider_result tenant_id=%s lead_id=%s email=%s followup_number=%s message_id=%s thread_id=%s raw_keys=%s",
                request.tenant.tenant_id,
                lead.id,
                lead.email,
                followup_number,
                result.message_id,
                result.thread_id,
                sorted(result.raw.keys()),
            )
            next_due = ""
            if followup_number < 3:
                next_due = (now_value + timedelta(days=2)).isoformat()
            await service.save_followup_result(
                tenant=request.tenant,
                lead=lead,
                last_email_id=getattr(last_email, "id", ""),
                subject=subject,
                body=body,
                thread_id=result.thread_id or thread_id,
                followup_number=followup_number,
                now_iso=now_value.isoformat(),
                now_dt=now_value,
                next_due_iso=next_due,
            )
            audit_log(
                LOGGER,
                logging.INFO,
                "OUTREACH_AUDIT followup.db_result tenant_id=%s lead_id=%s email=%s followup_number=%s status=persisted",
                request.tenant.tenant_id,
                lead.id,
                lead.email,
                followup_number,
            )
            sent += 1

        audit_log(
            LOGGER,
            logging.INFO,
            "OUTREACH_AUDIT followup.completed tenant_id=%s sent_followups=%s",
            request.tenant.tenant_id,
            sent,
        )
        run = AgentRun(
            tenant_id=request.tenant.tenant_id,
            agent_name=self.name,
            status="completed",
            input_payload=request.payload,
            output_payload={"sent_followups": sent},
        )
        await AgentRunService(db).record_run(request.tenant, run)
        return {"agent_run_id": run.id, "sent_followups": sent}


def _parse_datetime(value: str):
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
