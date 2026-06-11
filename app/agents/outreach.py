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
from app.services.plan_gate import PlanGateError, require_pro_plan
from app.services.provider_credential_service import ProviderCredentialService


LOGGER = logging.getLogger(__name__)


class OutreachAgent(BaseAgent):
    name = "outreach"

    async def run(self, request: AgentRequest, db: DatabaseSession | AsyncDatabaseSession) -> Dict[str, object]:
        service = OutreachService(db)
        blocked_domains = {
            item.strip().lower()
            for item in os.getenv("EMAIL_OPTOUT_DOMAINS", "").split(",")
            if item.strip()
        }
        email_service = OutreachEmailService(db)
        sent = 0
        failed = 0
        pending_leads = await service.list_pending_outreach_leads(request.tenant, blocked_domains)
        try:
            await require_pro_plan(db, request.tenant)
        except PlanGateError:
            for lead in pending_leads:
                await self._mark_failed(service, request.tenant, lead, "", "", "plan_locked")
                failed += 1
            await self._record_safe_failure_logs(request.tenant.tenant_id, pending_leads, "plan_locked")
            return await self._record_run(db, request, sent, failed)
        audit_log(
            LOGGER,
            logging.INFO,
            "OUTREACH_AUDIT outreach.lead_selection tenant_id=%s candidate_count=%s blocked_domain_count=%s",
            request.tenant.tenant_id,
            len(pending_leads),
            len(blocked_domains),
        )
        credential_service = ProviderCredentialService(db)
        credentials = await credential_service.get_gmail_credentials(request.tenant)
        if not credentials:
            for lead in pending_leads:
                await self._mark_failed(
                    service=service,
                    tenant=request.tenant,
                    lead=lead,
                    subject="",
                    body="",
                    reason="gmail_not_connected",
                )
                failed += 1
            await self._record_safe_failure_logs(request.tenant.tenant_id, pending_leads, "gmail_not_connected")
            return await self._record_run(db, request, sent, failed)
        if not str(credentials.get("refresh_token", "") or "").strip():
            for lead in pending_leads:
                await self._mark_failed(service, request.tenant, lead, "", "", "gmail_missing_refresh_token")
                failed += 1
            await self._record_safe_failure_logs(request.tenant.tenant_id, pending_leads, "gmail_missing_refresh_token")
            return await self._record_run(db, request, sent, failed)
        if not str(credentials.get("email_address", "") or credentials.get("sender_email", "") or "").strip():
            for lead in pending_leads:
                await self._mark_failed(service, request.tenant, lead, "", "", "gmail_sender_not_verified")
                failed += 1
            await self._record_safe_failure_logs(request.tenant.tenant_id, pending_leads, "gmail_sender_not_verified")
            return await self._record_run(db, request, sent, failed)
        audit_log(
            LOGGER,
            logging.INFO,
            "OUTREACH_AUDIT outreach.credentials_loaded tenant_id=%s has_refresh_token=%s has_access_token=%s",
            request.tenant.tenant_id,
            bool(credentials.get("refresh_token")),
            bool(credentials.get("access_token")),
        )
        provider = build_provider_registry()["gmail"]
        account = ProviderAccount(tenant_id=request.tenant.tenant_id, **credentials)
        for lead in pending_leads:
            await service.prepare_outreach_attempt(request.tenant, lead)
            lead.status = "pending"
            lead.outreach_status = "pending"
            lead.metadata = {
                key: value
                for key, value in dict(lead.metadata or {}).items()
                if key not in {"outreach_error", "outreach_error_at"}
            }
            audit_log(
                LOGGER,
                logging.INFO,
                "OUTREACH_AUDIT outreach.lead_selected tenant_id=%s lead_id=%s status=%s",
                request.tenant.tenant_id,
                lead.id,
                lead.status,
            )
            subject = ""
            body = ""
            recipient = service.outreach_recipient(lead)
            if not recipient:
                audit_log(
                    LOGGER,
                    logging.INFO,
                    "OUTREACH_AUDIT outreach.skipped tenant_id=%s lead_id=%s reason=no_verified_email",
                    request.tenant.tenant_id,
                    lead.id,
                )
                await self._mark_failed(service, request.tenant, lead, "", "", "no_verified_email")
                failed += 1
                continue
            try:
                try:
                    email_payload = await email_service.generate_outreach_email(request.tenant, lead)
                except Exception:
                    raise OutreachFailure("provider_generation_failed") from None
                subject = str(email_payload.get("subject", "") or "").strip()
                body = email_service.ensure_unsubscribe_footer(str(email_payload.get("body", "") or "").strip())
                audit_log(
                    LOGGER,
                    logging.INFO,
                    "OUTREACH_AUDIT outreach.email_generated tenant_id=%s lead_id=%s body_length=%s mode=%s provider=%s",
                    request.tenant.tenant_id,
                    lead.id,
                    len(body),
                    str(email_payload.get("mode", "") or ""),
                    str(email_payload.get("provider", "") or ""),
                )
                sent_at = datetime.now(timezone.utc)
                try:
                    provider_result = await provider.send(
                        account,
                        ProviderSendRequest(to=recipient, subject=subject, body=body),
                    )
                except Exception as error:
                    raise OutreachFailure(service.classify_send_failure(error)) from error
                audit_log(
                    LOGGER,
                    logging.INFO,
                    "OUTREACH_AUDIT outreach.provider_result tenant_id=%s lead_id=%s message_id=%s thread_id=%s raw_keys=%s",
                    request.tenant.tenant_id,
                    lead.id,
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
                    "OUTREACH_AUDIT outreach.db_result tenant_id=%s lead_id=%s status=sent",
                    request.tenant.tenant_id,
                    lead.id,
                )
                sent += 1
            except Exception as error:
                failed += 1
                reason = error.reason if isinstance(error, OutreachFailure) else "gmail_send_failed"
                audit_log(
                    LOGGER,
                    logging.ERROR,
                    "OUTREACH_AUDIT outreach.failed tenant_id=%s lead_id=%s error_type=%s status=failed",
                    request.tenant.tenant_id,
                    lead.id,
                    reason,
                    exc_info=True,
                )
                production_error_log(
                    LOGGER,
                    "Outreach failed tenant_id=%s lead_id=%s error_type=%s status=failed",
                    request.tenant.tenant_id,
                    lead.id,
                    reason,
                )
                await self._mark_failed(service, request.tenant, lead, subject, body, reason)
        return await self._record_run(db, request, sent, failed)

    async def _mark_failed(
        self,
        service: OutreachService,
        tenant,
        lead,
        subject: str,
        body: str,
        reason: str,
    ) -> None:
        now = datetime.now(timezone.utc)
        await service.mark_outreach_result(
            tenant=tenant,
            lead=lead,
            subject=subject,
            body=body,
            message_id="",
            thread_id="",
            status="failed",
            sent_at_iso=now.isoformat(),
            sent_at_dt=now,
            error_reason=reason,
        )

    async def _record_safe_failure_logs(self, tenant_id: str, leads, reason: str) -> None:
        for lead in leads:
            production_error_log(
                LOGGER,
                "Outreach failed tenant_id=%s lead_id=%s error_type=%s status=failed",
                tenant_id,
                lead.id,
                reason,
            )

    async def _record_run(
        self,
        db: DatabaseSession | AsyncDatabaseSession,
        request: AgentRequest,
        sent: int,
        failed: int,
    ) -> Dict[str, object]:
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


class OutreachFailure(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
