"""Read-only cross-tenant analytics for admin users."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List

from app.core.models import Tenant
from app.db.session import AsyncDatabaseSession, DatabaseSession
from app.services._async import maybe_await
from app.services.outreach_errors import normalized_outreach_error


def _iso(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value or "")


def _is_today(value: Any) -> bool:
    if not isinstance(value, datetime):
        return False
    return value.astimezone(timezone.utc).date() == datetime.now(timezone.utc).date()


def _plan(value: str) -> str:
    normalized = str(value or "").strip().title()
    return normalized if normalized in {"Free", "Pro", "Agency"} else "Free"


class AdminAnalyticsService:
    def __init__(self, db: DatabaseSession | AsyncDatabaseSession) -> None:
        self.db = db

    async def _all(self, repository_name: str) -> List[Any]:
        repository = getattr(self.db, repository_name)
        return list(await maybe_await(repository.list_all()))

    async def _tenants(self) -> List[Tenant]:
        return list(await maybe_await(self.db.tenants.list_all()))

    async def summary(self) -> Dict[str, int]:
        tenants = await self._tenants()
        users = await self._all("users")
        leads = await self._all("leads")
        jobs = await self._all("jobs")
        emails = await self._all("emails")
        replies = await self._all("replies")
        tenant_plan = {tenant.tenant_id: _plan(tenant.subscription_plan) for tenant in tenants}
        user_plan_counts = Counter(tenant_plan.get(user.tenant_id, "Free") for user in users)
        job_status_counts = Counter(str(job.status or "").strip().lower() for job in jobs)
        sent_emails = [
            email
            for email in emails
            if str(email.direction or "").lower() == "outbound" and str(email.status or "").lower() == "sent"
        ]
        return {
            "total_users": len(users),
            "total_tenants": len(tenants),
            "total_leads": len(leads),
            "total_jobs": len(jobs),
            "total_emails_sent": len(sent_emails),
            "total_replies": len(replies),
            "free_users": int(user_plan_counts.get("Free", 0)),
            "pro_users": int(user_plan_counts.get("Pro", 0)),
            "agency_users": int(user_plan_counts.get("Agency", 0)),
            "active_users_today": len([user for user in users if _is_today(user.updated_at)]),
            "leads_generated_today": len([lead for lead in leads if _is_today(lead.created_at)]),
            "queued_jobs": int(job_status_counts.get("queued", 0)),
            "running_jobs": int(job_status_counts.get("running", 0)),
            "failed_jobs": int(job_status_counts.get("failed", 0)),
        }

    async def recent_users(self, limit: int = 50) -> List[Dict[str, Any]]:
        tenants = await self._tenants()
        tenant_plan = {tenant.tenant_id: _plan(tenant.subscription_plan) for tenant in tenants}
        users = sorted(await self._all("users"), key=lambda item: item.created_at, reverse=True)[:limit]
        return [
            {
                "email": user.email,
                "tenant_id": user.tenant_id,
                "plan": tenant_plan.get(user.tenant_id, "Free"),
                "created_at": _iso(user.created_at),
                "last_activity": _iso(user.updated_at) or "unknown",
            }
            for user in users
        ]

    async def tenant_usage(self, limit: int = 100) -> List[Dict[str, Any]]:
        tenants = sorted(await self._tenants(), key=lambda item: item.created_at, reverse=True)[:limit]
        leads = await self._all("leads")
        jobs = await self._all("jobs")
        emails = await self._all("emails")
        replies = await self._all("replies")
        lead_counts = Counter(item.tenant_id for item in leads)
        job_counts = Counter(item.tenant_id for item in jobs)
        email_counts = Counter(item.tenant_id for item in emails)
        reply_counts = Counter(item.tenant_id for item in replies)
        latest_jobs: Dict[str, Any] = {}
        for job in sorted(jobs, key=lambda item: item.updated_at, reverse=True):
            latest_jobs.setdefault(job.tenant_id, job)
        return [
            {
                "tenant_id": tenant.tenant_id,
                "plan": _plan(tenant.subscription_plan),
                "lead_count": int(lead_counts.get(tenant.tenant_id, 0)),
                "job_count": int(job_counts.get(tenant.tenant_id, 0)),
                "email_count": int(email_counts.get(tenant.tenant_id, 0)),
                "reply_count": int(reply_counts.get(tenant.tenant_id, 0)),
                "last_job_status": str(getattr(latest_jobs.get(tenant.tenant_id), "status", "") or ""),
            }
            for tenant in tenants
        ]

    async def recent_leads(self, limit: int = 50) -> List[Dict[str, Any]]:
        leads = sorted(await self._all("leads"), key=lambda item: item.created_at, reverse=True)[:limit]
        return [
            {
                "tenant_id": lead.tenant_id,
                "company_url": lead.company_url or lead.website,
                "country": lead.country,
                "verified_email": lead.verified_email or lead.email,
                "verified_phone": lead.phone,
                "industry": lead.industry,
                "outreach_status": lead.outreach_status,
                "created_at": _iso(lead.created_at),
            }
            for lead in leads
        ]

    async def recent_jobs(self, limit: int = 50) -> List[Dict[str, Any]]:
        jobs = sorted(await self._all("jobs"), key=lambda item: item.updated_at, reverse=True)[:limit]
        return [
            {
                "tenant_id": job.tenant_id,
                "job_type": job.name or job.job_type,
                "status": job.status,
                "created_at": _iso(job.created_at),
                "updated_at": _iso(job.updated_at),
                "error": job.error,
            }
            for job in jobs
        ]

    async def outreach_stats(self) -> Dict[str, Any]:
        leads = await self._all("leads")
        emails = await self._all("emails")
        pending_verified_leads = [
            lead
            for lead in leads
            if str(lead.verified_email or lead.email or "").strip()
            and str(lead.outreach_status or lead.status or "").strip().lower() == "pending"
        ]
        blocked_leads = [
            lead
            for lead in leads
            if str(lead.outreach_status or lead.status or "").strip().lower() in {"blocked", "blocked_site"}
            or normalized_outreach_error(
                str(dict(lead.metadata or {}).get("outreach_error", "") or ""),
                status=lead.status,
                outreach_status=lead.outreach_status,
            )
            == "gmail_api_disabled"
        ]
        failed_reasons = Counter()
        for email in emails:
            if str(email.status or "").strip().lower() != "failed":
                continue
            metadata = dict(email.metadata or {})
            reason = normalized_outreach_error(
                str(metadata.get("error", "") or metadata.get("outreach_error", "") or ""),
                status=email.status,
            )
            failed_reasons[reason or "unknown_outreach_failure"] += 1
        sent_emails = [
            email
            for email in emails
            if str(email.direction or "").strip().lower() == "outbound"
            and str(email.status or "").strip().lower() == "sent"
        ]
        return {
            "pending_verified_leads": len(pending_verified_leads),
            "sent_emails": len(sent_emails),
            "failed_emails_by_reason": dict(failed_reasons),
            "blocked_leads": len(blocked_leads),
        }
