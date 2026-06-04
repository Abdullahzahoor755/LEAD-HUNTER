"""Dev/admin cleanup helpers for stale persisted local data."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
import re
from typing import Any, Sequence
from urllib.parse import urlparse

from app.core.models import Job, Lead, TenantContext
from app.db.session import AsyncDatabaseSession, DatabaseSession
from app.services._async import maybe_await
from app.services.lead_service import LeadService


SCORE_BREAKDOWN_MARKERS = (
    "email=40/40",
    "phone=25/25",
    "relevance=",
    "quality=",
    "minimum_fallback=",
    "missing email -> marked no_email",
)


@dataclass(slots=True)
class LeadCleanupAction:
    lead_id: str
    company_url: str
    action: str
    reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class LeadCleanupReport:
    tenant_id: str
    mode: str
    total_leads: int
    score_breakdown_count: int
    domain_mismatch_count: int
    suspicious_email_count: int
    duplicate_company_url_counts: dict[str, int]
    rows_to_clean: list[LeadCleanupAction]
    rows_to_delete: list[LeadCleanupAction]
    cleaned_count: int = 0
    deleted_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["rows_to_clean"] = [asdict(action) for action in self.rows_to_clean]
        payload["rows_to_delete"] = [asdict(action) for action in self.rows_to_delete]
        return payload


@dataclass(slots=True)
class JobCleanupAction:
    job_id: str
    name: str
    status: str
    created_at: str
    action: str
    payload_query: str = ""


@dataclass(slots=True)
class JobCleanupReport:
    tenant_id: str
    mode: str
    cutoff: datetime
    queued_jobs_older_than_cutoff: int
    jobs_to_cancel: list[JobCleanupAction]
    cancelled_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["cutoff"] = self.cutoff.isoformat()
        payload["jobs_to_cancel"] = [asdict(action) for action in self.jobs_to_cancel]
        return payload


def looks_like_score_breakdown(value: str) -> bool:
    lowered = str(value or "").lower()
    return any(marker in lowered for marker in SCORE_BREAKDOWN_MARKERS)


def _normalize_email(value: str) -> str:
    candidate = str(value or "").strip().lower()
    if not candidate:
        return ""
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", candidate):
        return ""
    return candidate


def _domain_from_url(value: str) -> str:
    candidate = str(value or "").strip()
    if not candidate:
        return ""
    parsed = urlparse(candidate if "://" in candidate else f"https://{candidate}")
    domain = parsed.netloc.lower().strip()
    return domain[4:] if domain.startswith("www.") else domain


def _root_domain(domain: str) -> str:
    cleaned = str(domain or "").lower().strip().strip(".")
    if cleaned.startswith("www."):
        cleaned = cleaned[4:]
    parts = [part for part in cleaned.split(".") if part]
    if len(parts) < 2:
        return ""
    compound_suffixes = {"com.au", "com.sa", "co.uk", "com.pk", "com.br", "com.tr", "com.sg", "co.in", "co.za", "com.my"}
    suffix = ".".join(parts[-2:])
    if suffix in compound_suffixes and len(parts) >= 3:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _is_suspicious_email_domain(domain: str) -> bool:
    parts = [part for part in str(domain or "").lower().split(".") if part]
    if len(parts) < 2:
        return True
    root_label = parts[-2]
    tld = parts[-1]
    return len(root_label) < 2 or len(tld) < 2 or not all(part.isalnum() or "-" in part for part in parts)


def _email_cleanup_reason(company_url: str, email: str) -> str:
    normalized = _normalize_email(email)
    if not normalized:
        return ""
    email_domain = normalized.split("@", 1)[-1]
    if _is_suspicious_email_domain(email_domain):
        return "suspicious_email_domain"
    company_root = _root_domain(_domain_from_url(company_url))
    email_root = _root_domain(email_domain)
    if company_root and email_root and company_root != email_root:
        return "domain_mismatch"
    return ""


def _append_rejected_email(metadata: dict[str, Any], email: str, reason: str) -> None:
    rejected = metadata.get("rejected_emails", [])
    if not isinstance(rejected, list):
        rejected = []
    entry = {"email": email, "reason": reason}
    if entry not in rejected:
        rejected.append(entry)
    metadata["rejected_emails"] = rejected


def _duplicate_extras(leads: Sequence[Lead]) -> set[str]:
    seen: set[str] = set()
    duplicate_ids: set[str] = set()
    for lead in sorted(leads, key=lambda item: item.created_at or datetime.min.replace(tzinfo=timezone.utc)):
        company_url = str(lead.company_url or "").strip()
        if not company_url:
            continue
        if company_url in seen:
            duplicate_ids.add(lead.id)
        seen.add(company_url)
    return duplicate_ids


async def cleanup_stale_leads(
    db: DatabaseSession | AsyncDatabaseSession,
    tenant: TenantContext,
    *,
    apply: bool = False,
    delete_stale: bool = False,
) -> LeadCleanupReport:
    scoped = db.for_tenant(tenant)
    leads = list(await maybe_await(scoped.list("leads")))
    duplicate_counts = {url: count for url, count in Counter(lead.company_url for lead in leads if lead.company_url).items() if count > 1}
    duplicate_ids = _duplicate_extras(leads)
    service = LeadService(db)

    rows_to_clean: list[LeadCleanupAction] = []
    rows_to_delete: list[LeadCleanupAction] = []
    score_breakdown_count = 0
    domain_mismatch_count = 0
    suspicious_email_count = 0
    cleaned_count = 0
    deleted_count = 0

    for lead in leads:
        reasons: list[str] = []
        if looks_like_score_breakdown(lead.service_reason):
            score_breakdown_count += 1
            reasons.append("score_breakdown_service_reason")
        email_reason = _email_cleanup_reason(lead.company_url, lead.verified_email)
        if email_reason == "domain_mismatch":
            domain_mismatch_count += 1
            reasons.append(email_reason)
        elif email_reason == "suspicious_email_domain":
            suspicious_email_count += 1
            reasons.append(email_reason)
        if not str(lead.industry or "").strip() or str(lead.industry or "").strip().lower() in {"unknown", "n/a", "na", "none"}:
            reasons.append("empty_or_unknown_industry")
        if lead.id in duplicate_ids:
            reasons.append("duplicate_company_url")

        if not reasons:
            continue

        action = LeadCleanupAction(
            lead_id=lead.id,
            company_url=lead.company_url,
            action="delete" if delete_stale else "clean",
            reasons=reasons,
        )
        if delete_stale:
            rows_to_delete.append(action)
            if apply:
                await maybe_await(scoped.delete("leads", lead.id))
                deleted_count += 1
            continue

        rows_to_clean.append(action)
        if not apply:
            continue

        metadata = dict(lead.metadata or {})
        if "score_breakdown_service_reason" in reasons:
            metadata.setdefault("score_breakdown", lead.service_reason)
            lead.service_reason = ""
            if looks_like_score_breakdown(lead.reason):
                metadata.setdefault("legacy_reason_score_breakdown", lead.reason)
                lead.reason = ""
        if email_reason in {"domain_mismatch", "suspicious_email_domain"}:
            _append_rejected_email(metadata, lead.verified_email, email_reason)
            if lead.email == lead.verified_email:
                lead.email = ""
            lead.verified_email = ""
        if "empty_or_unknown_industry" in reasons:
            lead.industry = service._normalize_industry(lead.industry)
        lead.metadata = metadata
        await maybe_await(scoped.save("leads", lead))
        cleaned_count += 1

    return LeadCleanupReport(
        tenant_id=tenant.tenant_id,
        mode="apply" if apply else "dry-run",
        total_leads=len(leads),
        score_breakdown_count=score_breakdown_count,
        domain_mismatch_count=domain_mismatch_count,
        suspicious_email_count=suspicious_email_count,
        duplicate_company_url_counts=duplicate_counts,
        rows_to_clean=rows_to_clean,
        rows_to_delete=rows_to_delete,
        cleaned_count=cleaned_count,
        deleted_count=deleted_count,
    )


async def cleanup_stale_jobs(
    db: DatabaseSession | AsyncDatabaseSession,
    tenant: TenantContext,
    *,
    cutoff: datetime | None = None,
    older_than_days: int = 1,
    apply: bool = False,
    status: str = "cancelled",
) -> JobCleanupReport:
    cutoff = cutoff or datetime.now(timezone.utc) - timedelta(days=older_than_days)
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)
    scoped = db.for_tenant(tenant)
    jobs = list(await maybe_await(scoped.list("jobs")))
    actions: list[JobCleanupAction] = []
    cancelled_count = 0
    for job in jobs:
        created_at = job.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        if job.status != "queued" or created_at >= cutoff:
            continue
        actions.append(
            JobCleanupAction(
                job_id=job.id,
                name=job.name,
                status=job.status,
                created_at=created_at.isoformat(),
                action=f"mark_{status}",
                payload_query=str((job.payload or {}).get("query", "")),
            )
        )
        if apply:
            job.status = status
            job.error = f"Marked {status} by stale-job cleanup."
            job.locked_by = ""
            job.locked_at = None
            job.completed_at = datetime.now(timezone.utc)
            await maybe_await(scoped.save("jobs", job))
            cancelled_count += 1

    return JobCleanupReport(
        tenant_id=tenant.tenant_id,
        mode="apply" if apply else "dry-run",
        cutoff=cutoff,
        queued_jobs_older_than_cutoff=len(actions),
        jobs_to_cancel=actions,
        cancelled_count=cancelled_count,
    )
