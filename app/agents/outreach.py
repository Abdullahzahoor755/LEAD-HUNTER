"""Outreach agent for tenant-aware email orchestration."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import os
from typing import Dict

from app.agents.base import AgentRequest, BaseAgent
from app.core.models import AgentRun
from app.db.session import AsyncDatabaseSession, DatabaseSession
from app.providers.base import ProviderAccount, ProviderSendRequest
from app.providers.registry import build_provider_registry
from app.services.agent_run_service import AgentRunService
from app.services.outreach_audit import audit_log, production_error_log
from app.services.outreach_email_service import OutreachEmailService
from app.services.outreach_service import OutreachService
from app.services.plan_gate import require_pro_plan
from app.services.provider_credential_service import ProviderCredentialService


LOGGER = logging.getLogger(__name__)


class OutreachAgent(BaseAgent):
    name = "outreach"

    async def run(self, request: AgentRequest, db: DatabaseSession | AsyncDatabaseSession) -> Dict[str, object]:
        await require_pro_plan(db, request.tenant)
        service = OutreachService(db)
        credential_service = ProviderCredentialService(db)
        credentials = await credential_service.get_gmail_credentials(request.tenant)
        if not credentials:
            raise ValueError("Tenant Gmail credentials are not configured.")
        audit_log(
            LOGGER,
            logging.INFO,
            "OUTREACH_AUDIT outreach.credentials_loaded tenant_id=%s sender_email=%s has_refresh_token=%s has_access_token=%s",
            request.tenant.tenant_id,
            str(credentials.get("email_address", "") or ""),
            bool(credentials.get("refresh_token")),
            bool(credentials.get("access_token")),
        )
        provider = build_provider_registry()["gmail"]
        account = ProviderAccount(tenant_id=request.tenant.tenant_id, **credentials)
        blocked_domains = {
            item.strip().lower()
            for item in os.getenv("EMAIL_OPTOUT_DOMAINS", "").split(",")
            if item.strip()
        }
        email_service = OutreachEmailService(db)
        sent = 0
        failed = 0
        pending_leads = await service.list_pending_outreach_leads(request.tenant, blocked_domains)
        audit_log(
            LOGGER,
            logging.INFO,
            "OUTREACH_AUDIT outreach.lead_selection tenant_id=%s candidate_count=%s blocked_domain_count=%s",
            request.tenant.tenant_id,
            len(pending_leads),
            len(blocked_domains),
        )
        for lead in pending_leads:
            audit_log(
                LOGGER,
                logging.INFO,
                "OUTREACH_AUDIT outreach.lead_selected tenant_id=%s lead_id=%s email=%s status=%s",
                request.tenant.tenant_id,
                lead.id,
                lead.email,
                lead.status,
            )
            subject = ""
            body = ""
            try:
                email_payload = await email_service.generate_outreach_email(request.tenant, lead)
                subject = str(email_payload.get("subject", "") or "").strip()
                body = email_service.ensure_unsubscribe_footer(str(email_payload.get("body", "") or "").strip())
                audit_log(
                    LOGGER,
                    logging.INFO,
                    "OUTREACH_AUDIT outreach.email_generated tenant_id=%s lead_id=%s email=%s subject=%r body_length=%s mode=%s provider=%s",
                    request.tenant.tenant_id,
                    lead.id,
                    lead.email,
                    subject,
                    len(body),
                    str(email_payload.get("mode", "") or ""),
                    str(email_payload.get("provider", "") or ""),
                )
                sent_at = datetime.now(timezone.utc)
                provider_result = await provider.send(
                    account,
                    ProviderSendRequest(to=lead.email, subject=subject, body=body),
                )
                audit_log(
                    LOGGER,
                    logging.INFO,
                    "OUTREACH_AUDIT outreach.provider_result tenant_id=%s lead_id=%s email=%s message_id=%s thread_id=%s raw_keys=%s",
                    request.tenant.tenant_id,
                    lead.id,
                    lead.email,
                    provider_result.message_id,
                    provider_result.thread_id,
                    sorted(provider_result.raw.keys()),
                )
                await service.mark_outreach_result(
                    tenant=request.tenant,
                    lead=lead,
                    subject=subject,
                    body=body,
                    message_id=provider_result.message_id,
                    thread_id=provider_result.thread_id,
                    status="sent",
                    sent_at_iso=sent_at.isoformat(),
                    sent_at_dt=sent_at,
                )
                audit_log(
                    LOGGER,
                    logging.INFO,
                    "OUTREACH_AUDIT outreach.db_result tenant_id=%s lead_id=%s email=%s status=sent",
                    request.tenant.tenant_id,
                    lead.id,
                    lead.email,
                )
                sent += 1
            except Exception as error:
                failed += 1
                audit_log(
                    LOGGER,
                    logging.ERROR,
                    "OUTREACH_AUDIT outreach.failed tenant_id=%s lead_id=%s email=%s error=%s",
                    request.tenant.tenant_id,
                    lead.id,
                    lead.email,
                    error,
                    exc_info=True,
                )
                production_error_log(
                    LOGGER,
                    "Outreach failed tenant_id=%s lead_id=%s status=failed error_type=%s",
                    request.tenant.tenant_id,
                    lead.id,
                    type(error).__name__,
                )
                await service.mark_outreach_result(
                    tenant=request.tenant,
                    lead=lead,
                    subject=subject,
                    body=body,
                    message_id="",
                    thread_id="",
                    status="failed",
                    sent_at_iso=datetime.now(timezone.utc).isoformat(),
                    sent_at_dt=datetime.now(timezone.utc),
                )
        result = {"sent_messages": sent, "failed_messages": failed}
        audit_log(
            LOGGER,
            logging.INFO,
            "OUTREACH_AUDIT outreach.completed tenant_id=%s sent=%s failed=%s",
            request.tenant.tenant_id,
            sent,
            failed,
        )
        run = AgentRun(
            tenant_id=request.tenant.tenant_id,
            agent_name=self.name,
            status="completed",
            input_payload=request.payload,
            output_payload=result,
        )
        await AgentRunService(db).record_run(request.tenant, run)
        return {"agent_run_id": run.id, **result}
