"""Outreach service backed by tenant-aware repositories."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from app.core.models import Email, Followup, Lead, Reply, TenantContext
from app.db.session import AsyncDatabaseSession, DatabaseSession
from app.services.lead_service import LeadService
from app.services._async import maybe_await
import leads as legacy_leads


class OutreachService:
    def __init__(self, db: DatabaseSession | AsyncDatabaseSession) -> None:
        self.db = db
        self.lead_service = LeadService(db)

    async def send_pending_outreach(self, tenant: TenantContext) -> Dict[str, object]:
        sent = 0
        failed = 0
        scoped_db = self.db.for_tenant(tenant)
        sendable_statuses = {"pending", "draft", "no_content_scraped", "blocked_site", "slow_site", "js_site"}
        for lead in await maybe_await(scoped_db.list("leads")):
            if lead.status.lower() not in sendable_statuses or not lead.email:
                continue
            try:
                lead_dict = {
                    "Company": lead.company or lead.company_name or "",
                    "Website": lead.website or "",
                    "Reason": lead.reason or "",
                }
                subject, body = legacy_leads.generate_cold_email(lead_dict)
                body = legacy_leads.append_unsubscribe_footer(body)
                result = legacy_leads.send_email_gmail(lead.email, subject, body)
                gmail_message_id = str(result.get("id", ""))
                gmail_thread_id = str(result.get("threadId", ""))
                sent_at = datetime.now(timezone.utc)
                sent_at_iso = sent_at.isoformat()
                message = Email(
                    tenant_id=tenant.tenant_id,
                    campaign_id=lead.campaign_id,
                    lead_id=lead.id,
                    subject=subject,
                    body=body,
                    provider="gmail",
                    provider_message_id=gmail_message_id,
                    provider_thread_id=gmail_thread_id,
                    direction="outbound",
                    status="sent",
                    sent_at=sent_at,
                    metadata={"source": "outreach_service"},
                )
                await maybe_await(scoped_db.save("emails", message))
                metadata = dict(lead.metadata or {})
                metadata.update({
                    "EmailSubject": subject,
                    "LastEmailBody": body,
                    "EmailSentAt": sent_at_iso,
                    "GmailThreadId": gmail_thread_id,
                    "LastContactedAt": sent_at_iso,
                })
                lead.metadata = metadata
                lead.status = "sent"
                await self.lead_service.upsert_lead(tenant, lead)
                sent += 1
            except Exception:
                lead.status = "failed"
                await self.lead_service.upsert_lead(tenant, lead)
                failed += 1
                continue
        return {"sent_messages": sent, "failed_messages": failed}

    async def list_pending_outreach_leads(self, tenant: TenantContext, blocked_domains: set[str]) -> List[Lead]:
        sendable_statuses = {"pending", "draft", "no_content_scraped", "blocked_site", "slow_site", "js_site"}
        leads = await self.lead_service.list_leads(tenant)
        return [
            lead
            for lead in leads
            if lead.email
            and lead.status.lower() in sendable_statuses
            and lead.status.lower() != "unsubscribed"
            and self._email_domain(lead.email) not in blocked_domains
        ]

    async def mark_outreach_result(
        self,
        tenant: TenantContext,
        lead: Lead,
        subject: str,
        body: str,
        message_id: str,
        thread_id: str,
        status: str,
        sent_at_iso: str,
        sent_at_dt: datetime,
    ) -> None:
        scoped = self.db.for_tenant(tenant)
        existing = await maybe_await(scoped.get("leads", lead.id))
        if existing is None:
            return
        metadata = dict(existing.metadata or {})
        metadata.update(
            {
                "EmailSubject": subject,
                "LastEmailBody": body,
                "EmailSentAt": sent_at_iso,
                "GmailThreadId": thread_id,
                "LastContactedAt": sent_at_iso,
            }
        )
        existing.status = status
        existing.metadata = metadata
        await self.lead_service.upsert_lead(tenant, existing)
        email_record = Email(
            tenant_id=tenant.tenant_id,
            campaign_id=lead.campaign_id,
            lead_id=lead.id,
            subject=subject,
            body=body,
            provider="gmail",
            provider_message_id=message_id,
            provider_thread_id=thread_id,
            direction="outbound",
            status=status,
            sent_at=sent_at_dt,
            metadata={"source": "legacy_outreach"},
        )
        await maybe_await(scoped.save("emails", email_record))

    async def list_followup_candidates(self, tenant: TenantContext, blocked_domains: set[str], now_value: datetime, parse_datetime) -> List[Dict[str, Any]]:
        scoped = self.db.for_tenant(tenant)
        items: List[Dict[str, Any]] = []
        for lead in await self.lead_service.list_leads(tenant):
            if lead.status.lower() != "sent" or lead.status.lower() == "unsubscribed":
                continue
            if self._email_domain(lead.email) in blocked_domains:
                continue
            metadata = dict(lead.metadata or {})
            if str(metadata.get("ReplyStatus", "")).strip().lower() == "received":
                continue
            followup_count = int(metadata.get("FollowupCount", 0) or 0)
            if followup_count >= 3:
                continue
            sent_at = parse_datetime(str(metadata.get("LastContactedAt", "") or metadata.get("EmailSentAt", "")))
            if not sent_at:
                continue
            next_due = parse_datetime(str(metadata.get("NextFollowupDue", "")))
            if next_due and now_value < next_due:
                continue
            if next_due is None and now_value < sent_at + timedelta(days=2):
                continue
            emails = await maybe_await(scoped.list("emails"))
            lead_emails = [item for item in emails if item.lead_id == lead.id]
            lead_emails.sort(key=lambda item: (item.sent_at or datetime.min.replace(tzinfo=timezone.utc), item.created_at), reverse=True)
            last_email = lead_emails[0] if lead_emails else None
            items.append({"lead": lead, "last_email": last_email})
        return items

    async def save_followup_result(
        self,
        tenant: TenantContext,
        lead: Lead,
        last_email_id: str,
        subject: str,
        body: str,
        thread_id: str,
        followup_number: int,
        now_iso: str,
        now_dt: datetime,
        next_due_iso: str,
    ) -> None:
        scoped = self.db.for_tenant(tenant)
        existing = await maybe_await(scoped.get("leads", lead.id))
        if existing is None:
            return
        metadata = dict(existing.metadata or {})
        metadata.update(
            {
                "FollowupCount": followup_number,
                "LastFollowupDate": now_iso,
                "NextFollowupDue": next_due_iso,
                "LastContactedAt": now_iso,
                "LastEmailBody": body,
                "EmailSubject": subject,
                "GmailThreadId": thread_id or str(metadata.get("GmailThreadId", "")),
            }
        )
        existing.metadata = metadata
        await self.lead_service.upsert_lead(tenant, existing)
        followup = Followup(
            tenant_id=tenant.tenant_id,
            campaign_id=lead.campaign_id,
            lead_id=lead.id,
            email_id=last_email_id,
            sequence_step=followup_number,
            status="sent",
            sent_at=now_dt,
            metadata={"thread_id": thread_id},
        )
        await maybe_await(scoped.save("followups", followup))

    async def list_reply_candidates(self, tenant: TenantContext) -> List[Dict[str, Any]]:
        scoped = self.db.for_tenant(tenant)
        items: List[Dict[str, Any]] = []
        emails = await maybe_await(scoped.list("emails"))
        for lead in await self.lead_service.list_leads(tenant):
            if lead.status.lower() != "sent":
                continue
            metadata = dict(lead.metadata or {})
            thread_id = str(metadata.get("GmailThreadId", "")).strip()
            if not thread_id:
                continue
            lead_emails = [item for item in emails if item.lead_id == lead.id]
            lead_emails.sort(key=lambda item: (item.sent_at or datetime.min.replace(tzinfo=timezone.utc), item.created_at), reverse=True)
            last_email = lead_emails[0] if lead_emails else None
            items.append({"lead": lead, "thread_id": thread_id, "last_email": last_email})
        return items

    async def save_reply_result(
        self,
        tenant: TenantContext,
        lead: Lead,
        email_id: str,
        message: Dict[str, Any],
        updates: Dict[str, str],
        received_at,
    ) -> None:
        scoped = self.db.for_tenant(tenant)
        existing = await maybe_await(scoped.get("leads", lead.id))
        if existing is None:
            return
        metadata = dict(existing.metadata or {})
        metadata.update(updates)
        existing.metadata = metadata
        await self.lead_service.upsert_lead(tenant, existing)
        reply = Reply(
            tenant_id=tenant.tenant_id,
            campaign_id=lead.campaign_id,
            lead_id=lead.id,
            email_id=email_id,
            provider_message_id=str(message.get("id", "")),
            provider_thread_id=str(message.get("threadId", "")),
            from_email=str(updates.get("LastReplyFrom", "")),
            subject=str(updates.get("ReplyClassification", "")),
            body=str(updates.get("LastReplySnippet", "")),
            classification=str(updates.get("ReplyClassification", "")),
            sentiment=str(updates.get("Sentiment", "")),
            lead_temperature=str(updates.get("LeadTemperature", "")),
            received_at=received_at,
            metadata={
                "LastReply": str(updates.get("LastReply", "")),
                "ReplyConfidenceScore": str(updates.get("ReplyConfidenceScore", "")),
                "NextActionSuggestion": str(updates.get("NextActionSuggestion", "")),
            },
        )
        await maybe_await(scoped.save("replies", reply))

    def _email_domain(self, email: str) -> str:
        return str(email or "").strip().lower().split("@")[-1]

    def _ensure_unsubscribe_footer(self, body: str) -> str:
        footer = "If you prefer not to hear from us again, reply with unsubscribe."
        normalized = str(body or "").rstrip()
        if footer.lower() in normalized.lower():
            return normalized
        return f"{normalized}\n\n{footer}"
