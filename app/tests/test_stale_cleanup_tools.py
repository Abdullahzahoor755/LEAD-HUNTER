from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.agents.base import AgentRequest, BaseAgent
from app.agents.registry import AgentRegistry
from app.core.models import Job, Lead, Tenant, TenantContext
from app.db.session import build_memory_session
from app.services.stale_data_cleanup import cleanup_stale_jobs, cleanup_stale_leads
from app.workers.jobs import AsyncJobQueue


class _EchoAgent(BaseAgent):
    name = "lead_generation"

    async def run(self, request: AgentRequest, db) -> dict[str, object]:
        return {"query": request.payload.get("query", "")}


@pytest.mark.anyio
async def test_stale_service_reason_cleanup_moves_score_breakdown_to_metadata() -> None:
    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-clean-score")
    db.tenants.save(Tenant(tenant_id=tenant.tenant_id, name="Tenant", slug="tenant-clean-score"))
    lead = db.for_tenant(tenant).save(
        "leads",
        Lead(
            tenant_id=tenant.tenant_id,
            company_url="https://score.test",
            service_reason="email=40/40 | phone=25/25 | relevance=0/20",
            reason="email=40/40 | phone=25/25 | relevance=0/20",
            industry="",
        ),
    )

    report = await cleanup_stale_leads(db, tenant, apply=True)
    saved = db.for_tenant(tenant).get("leads", lead.id)

    assert report.cleaned_count == 1
    assert report.score_breakdown_count == 1
    assert saved.service_reason == ""
    assert saved.reason == ""
    assert saved.metadata["score_breakdown"] == "email=40/40 | phone=25/25 | relevance=0/20"
    assert saved.metadata["legacy_reason_score_breakdown"] == "email=40/40 | phone=25/25 | relevance=0/20"
    assert saved.industry == "Other"


@pytest.mark.anyio
async def test_cleanup_prevents_restart_backfill_from_restoring_score_breakdown() -> None:
    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-clean-restart")
    db.tenants.save(Tenant(tenant_id=tenant.tenant_id, name="Tenant", slug="tenant-clean-restart"))
    lead = db.for_tenant(tenant).save(
        "leads",
        Lead(
            tenant_id=tenant.tenant_id,
            company_url="https://restart.test",
            service_reason="email=40/40 | phone=25/25 | relevance=0/20",
            reason="email=40/40 | phone=25/25 | relevance=0/20",
        ),
    )

    await cleanup_stale_leads(db, tenant, apply=True)
    saved = db.for_tenant(tenant).get("leads", lead.id)

    if saved.service_reason == "" and saved.reason:
        saved.service_reason = saved.reason

    assert saved.service_reason == ""
    assert saved.reason == ""


@pytest.mark.anyio
async def test_domain_mismatch_cleanup_rejects_email_without_deleting_lead() -> None:
    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-clean-domain")
    db.tenants.save(Tenant(tenant_id=tenant.tenant_id, name="Tenant", slug="tenant-clean-domain"))
    lead = db.for_tenant(tenant).save(
        "leads",
        Lead(
            tenant_id=tenant.tenant_id,
            company_url="https://energycities.org",
            verified_email="info@pacificenergy.com.au",
            email="info@pacificenergy.com.au",
            service_reason="valid reason",
            industry="energy",
        ),
    )

    report = await cleanup_stale_leads(db, tenant, apply=True)
    saved = db.for_tenant(tenant).get("leads", lead.id)

    assert report.domain_mismatch_count == 1
    assert saved.verified_email == ""
    assert saved.email == ""
    assert saved.metadata["rejected_emails"] == [{"email": "info@pacificenergy.com.au", "reason": "domain_mismatch"}]


@pytest.mark.anyio
async def test_suspicious_email_cleanup_rejects_short_domain() -> None:
    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-clean-suspicious")
    db.tenants.save(Tenant(tenant_id=tenant.tenant_id, name="Tenant", slug="tenant-clean-suspicious"))
    lead = db.for_tenant(tenant).save(
        "leads",
        Lead(
            tenant_id=tenant.tenant_id,
            company_url="https://sa.mustakbil.com",
            verified_email="jobs@m.al",
            email="jobs@m.al",
            service_reason="valid reason",
            industry="jobs",
        ),
    )

    report = await cleanup_stale_leads(db, tenant, apply=True)
    saved = db.for_tenant(tenant).get("leads", lead.id)

    assert report.suspicious_email_count == 1
    assert saved.verified_email == ""
    assert saved.email == ""
    assert saved.metadata["rejected_emails"] == [{"email": "jobs@m.al", "reason": "suspicious_email_domain"}]


@pytest.mark.anyio
async def test_dry_run_does_not_modify_leads() -> None:
    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-dry-run")
    db.tenants.save(Tenant(tenant_id=tenant.tenant_id, name="Tenant", slug="tenant-dry-run"))
    lead = db.for_tenant(tenant).save(
        "leads",
        Lead(
            tenant_id=tenant.tenant_id,
            company_url="https://dry-run.test",
            verified_email="lead@other.test",
            service_reason="email=40/40 | phone=25/25",
            industry="",
        ),
    )

    report = await cleanup_stale_leads(db, tenant, apply=False)
    saved = db.for_tenant(tenant).get("leads", lead.id)

    assert report.cleaned_count == 0
    assert len(report.rows_to_clean) == 1
    assert saved.service_reason == "email=40/40 | phone=25/25"
    assert saved.verified_email == "lead@other.test"
    assert saved.industry == ""


@pytest.mark.anyio
async def test_old_queued_job_cleanup_marks_only_old_queued_jobs() -> None:
    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-clean-jobs")
    db.tenants.save(Tenant(tenant_id=tenant.tenant_id, name="Tenant", slug="tenant-clean-jobs"))
    old_job = db.for_tenant(tenant).save(
        "jobs",
        Job(
            tenant_id=tenant.tenant_id,
            name="lead_generation",
            status="queued",
            payload={"query": "old"},
            created_at=datetime.now(timezone.utc) - timedelta(days=3),
        ),
    )
    fresh_job = db.for_tenant(tenant).save(
        "jobs",
        Job(tenant_id=tenant.tenant_id, name="lead_generation", status="queued", payload={"query": "fresh"}),
    )
    completed_job = db.for_tenant(tenant).save(
        "jobs",
        Job(
            tenant_id=tenant.tenant_id,
            name="lead_generation",
            status="completed",
            payload={"query": "completed"},
            created_at=datetime.now(timezone.utc) - timedelta(days=3),
        ),
    )

    report = await cleanup_stale_jobs(db, tenant, older_than_days=1, apply=True)

    assert report.cancelled_count == 1
    assert db.for_tenant(tenant).get("jobs", old_job.id).status == "cancelled"
    assert db.for_tenant(tenant).get("jobs", fresh_job.id).status == "queued"
    assert db.for_tenant(tenant).get("jobs", completed_job.id).status == "completed"


@pytest.mark.anyio
async def test_run_once_can_prefer_newest_matching_queued_job() -> None:
    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-run-newest")
    db.tenants.save(Tenant(tenant_id=tenant.tenant_id, name="Tenant", slug="tenant-run-newest"))
    old_job = db.for_tenant(tenant).save(
        "jobs",
        Job(
            tenant_id=tenant.tenant_id,
            name="lead_generation",
            status="queued",
            payload={"query": "old"},
            created_at=datetime.now(timezone.utc) - timedelta(days=2),
        ),
    )
    new_job = db.for_tenant(tenant).save(
        "jobs",
        Job(tenant_id=tenant.tenant_id, name="lead_generation", status="queued", payload={"query": "new"}),
    )
    registry = AgentRegistry()
    registry.register(_EchoAgent())
    queue = AsyncJobQueue(db=db, agents=registry)

    result = await queue.run_once_for_tenant(tenant, job_type="lead_generation")

    assert result["status"] == "completed"
    assert result["job_id"] == new_job.id
    assert db.for_tenant(tenant).get("jobs", new_job.id).result == {"query": "new"}
    assert db.for_tenant(tenant).get("jobs", old_job.id).status == "queued"
