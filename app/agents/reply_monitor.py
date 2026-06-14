"""Tenant-safe Gmail reply monitor agent."""

from __future__ import annotations

import logging
from typing import Any, Dict

import leads as legacy_leads

from app.agents.base import AgentRequest, BaseAgent
from app.core.models import AgentRun
from app.db.session import AsyncDatabaseSession, DatabaseSession
from app.providers.base import ProviderAccount
from app.providers.registry import build_provider_registry
from app.services.agent_run_service import AgentRunService
from app.services.outreach_audit import audit_log
from app.services.outreach_service import OutreachService
from app.services.plan_gate import require_pro_plan
from app.services.provider_credential_service import ProviderCredentialService
from app.services.reply_ai_service import ReplyAiService


LOGGER = logging.getLogger(__name__)


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
        audit_log(
            LOGGER,
            logging.INFO,
            "OUTREACH_AUDIT reply.credentials_loaded tenant_id=%s sender_email=%s has_refresh_token=%s has_access_token=%s",
            request.tenant.tenant_id,
            str(credentials.get("email_address", "") or ""),
            bool(credentials.get("refresh_token")),
            bool(credentials.get("access_token")),
        )
        account = ProviderAccount(tenant_id=request.tenant.tenant_id, **credentials)
        processed = 0
        my_email = str(credentials.get("email_address", "")).strip().lower()
        candidates = await service.list_reply_candidates(request.tenant)
        audit_log(
            LOGGER,
            logging.INFO,
            "OUTREACH_AUDIT reply.candidates tenant_id=%s candidate_count=%s",
            request.tenant.tenant_id,
            len(candidates),
        )
        for item in candidates:
            lead = item["lead"]
            thread_id = item["thread_id"]
            audit_log(
                LOGGER,
                logging.INFO,
                "OUTREACH_AUDIT reply.check_thread tenant_id=%s lead_id=%s email=%s thread_id=%s",
                request.tenant.tenant_id,
                lead.id,
                lead.email,
                thread_id,
            )
            replies = await provider.fetch_replies(account, thread_id)
            audit_log(
                LOGGER,
                logging.INFO,
                "OUTREACH_AUDIT reply.provider_result tenant_id=%s lead_id=%s email=%s thread_id=%s fetched_count=%s",
                request.tenant.tenant_id,
                lead.id,
                lead.email,
                thread_id,
                len(replies),
            )
            if not replies:
                continue
            inbound = []
            for reply in replies:
                sender_email = legacy_leads.extract_email_address(reply.from_address)
                if my_email and sender_email == my_email:
                    continue
                inbound.append(reply)
            if not inbound:
                audit_log(
                    LOGGER,
                    logging.INFO,
                    "OUTREACH_AUDIT reply.no_inbound tenant_id=%s lead_id=%s email=%s thread_id=%s fetched_count=%s",
                    request.tenant.tenant_id,
                    lead.id,
                    lead.email,
                    thread_id,
                    len(replies),
                )
                continue
            bounces = [reply for reply in inbound if _is_bounce(reply)]
            if bounces:
                latest_bounce = bounces[-1]
                updates = {
                    "BounceStatus": "bounced",
                    "BounceReason": latest_bounce.subject or latest_bounce.body[:200],
                    "BounceDetectedAt": legacy_leads.parse_gmail_internal_date(dict(latest_bounce.metadata.get("raw", {}))),
                    "NextFollowupDue": "",
                }
                await service.save_reply_result(
                    tenant=request.tenant,
                    lead=lead,
                    email_id=getattr(item["last_email"], "id", ""),
                    message=dict(latest_bounce.metadata.get("raw", {})),
                    updates=updates,
                    received_at=legacy_leads.parse_datetime(str(updates.get("BounceDetectedAt", ""))),
                )
                processed += 1
                continue
            inbound = [reply for reply in inbound if not _is_auto_reply(reply)]
            if not inbound:
                audit_log(
                    LOGGER,
                    logging.INFO,
                    "OUTREACH_AUDIT reply.auto_reply_skipped tenant_id=%s lead_id=%s email=%s thread_id=%s",
                    request.tenant.tenant_id,
                    lead.id,
                    lead.email,
                    thread_id,
                )
                continue
            inbound.sort(key=lambda reply: int(str(reply.metadata.get("internal_date", "0")) or "0"))
            latest_reply = inbound[-1]
            last_reply_at = legacy_leads.parse_gmail_internal_date(dict(latest_reply.metadata.get("raw", {})))
            if last_reply_at and last_reply_at == str((lead.metadata or {}).get("LastReplyAt", "")):
                audit_log(
                    LOGGER,
                    logging.INFO,
                    "OUTREACH_AUDIT reply.duplicate_skipped tenant_id=%s lead_id=%s email=%s last_reply_at=%s",
                    request.tenant.tenant_id,
                    lead.id,
                    lead.email,
                    last_reply_at,
                )
                continue
            analysis = await ai_service.classify(
                company=lead.company,
                sender_email=legacy_leads.extract_email_address(latest_reply.from_address),
                subject=latest_reply.subject,
                reply_text=latest_reply.body,
            )
            audit_log(
                LOGGER,
                logging.INFO,
                "OUTREACH_AUDIT reply.analysis tenant_id=%s lead_id=%s email=%s from=%s classification=%s sentiment=%s",
                request.tenant.tenant_id,
                lead.id,
                lead.email,
                legacy_leads.extract_email_address(latest_reply.from_address),
                analysis.get("classification", ""),
                analysis.get("sentiment", ""),
            )
            updates: Dict[str, Any] = {
                "ReplyStatus": _reply_status_from_classification(str(analysis.get("classification", ""))),
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
            audit_log(
                LOGGER,
                logging.INFO,
                "OUTREACH_AUDIT reply.db_result tenant_id=%s lead_id=%s email=%s status=persisted",
                request.tenant.tenant_id,
                lead.id,
                lead.email,
            )
            processed += 1
        audit_log(
            LOGGER,
            logging.INFO,
            "OUTREACH_AUDIT reply.completed tenant_id=%s processed=%s",
            request.tenant.tenant_id,
            processed,
        )
        run = AgentRun(
            tenant_id=request.tenant.tenant_id,
            agent_name=self.name,
            status="completed",
            input_payload=request.payload,
            output_payload={"checked": processed},
        )
        await AgentRunService(db).record_run(request.tenant, run)
        return run.output_payload


def _reply_status_from_classification(classification: str) -> str:
    value = str(classification or "").strip().lower()
    if value == "interested":
        return "interested"
    if value in {"not interested", "not_interested"}:
        return "not_interested"
    return "replied"


def _is_bounce(reply) -> bool:
    sender = legacy_leads.extract_email_address(getattr(reply, "from_address", "")).lower()
    subject = str(getattr(reply, "subject", "") or "").lower()
    body = str(getattr(reply, "body", "") or "").lower()
    return (
        "mailer-daemon" in sender
        or "postmaster" in sender
        or "delivery status notification" in subject
        or "undeliverable" in subject
        or "delivery has failed" in body
        or "message not delivered" in body
    )


def _is_auto_reply(reply) -> bool:
    subject = str(getattr(reply, "subject", "") or "").lower()
    body = str(getattr(reply, "body", "") or "").lower()
    return any(
        marker in f"{subject}\n{body}"
        for marker in ("out of office", "automatic reply", "auto-reply", "autoreply", "vacation responder")
    )
