from __future__ import annotations

import httpx
import pytest

import app.agents.outreach as outreach_module
import leads as legacy_leads
from app.agents.base import AgentRequest
from app.agents.lead_generation import LeadGenerationAgent
from app.agents.lead_pipeline import ScoringAgent, ScraperAgent
from app.agents.outreach import OutreachAgent
from app.api.app import create_fastapi_app
from app.core.models import Job, Lead, Tenant, TenantContext
from app.db.session import build_memory_session
from app.providers.base import ProviderSendResult
from app.services.auth_service import AuthService
from app.services.lead_service import LeadService
from app.services.outreach_service import OutreachService
from app.services.provider_credential_service import ProviderCredentialService


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_it_pakistan_query_builder_and_variants() -> None:
    assert legacy_leads.build_search_query("IT", "Pakistan") == "IT companies in Pakistan"
    variants = legacy_leads.build_search_query_variants("IT", "Pakistan")
    assert "IT companies in Pakistan" in variants
    assert "software houses in Pakistan" in variants
    assert "custom software development companies in Pakistan" in variants
    assert len(variants) >= 6


def test_search_result_filter_rejects_bad_sources_and_keeps_company() -> None:
    results = [
        {"title": "Developer tools", "link": "https://github.com/example", "snippet": "code"},
        {"title": "Pakistan Embassy", "link": "https://example.org/embassy", "snippet": "government consulate"},
        {"title": "Acme Software Company", "link": "https://acmesoft.example/services", "snippet": "software development company"},
    ]
    websites, stats = legacy_leads.extract_websites_with_stats(results)
    assert websites == ["https://acmesoft.example"]
    assert stats["rejected_bad_domain_count"] >= 2
    assert any(event.get("reason") == "rejected_bad_domain_github" for event in stats["events"])


def test_lead_quality_grade_and_email_quality_helpers() -> None:
    grade = legacy_leads.lead_quality_grade(
        "https://acmesoft.example",
        {"industry": "Software development", "contact_page": "https://acmesoft.example/contact"},
        email="info@acmesoft.example",
        phone="+123",
    )
    assert grade == "A"


def test_gmail_api_disabled_is_specific_safe_reason() -> None:
    reason = OutreachService(build_memory_session()).classify_send_failure(
        RuntimeError("HttpError 403 accessNotConfigured Gmail API has not been used in project before or it is disabled")
    )
    assert reason == "gmail_api_disabled"


@pytest.mark.anyio
async def test_missing_serper_key_returns_clear_lead_generation_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-missing-serper")
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    monkeypatch.setattr(legacy_leads, "load_environment", lambda: None)

    result = await LeadGenerationAgent().run(
        AgentRequest(tenant=tenant, payload={"query": "software companies in Pakistan", "limit": 1}),
        db,
    )

    assert result["status"] == "FAILED"
    assert "SERPER_API_KEY" in result["message"]
    assert result["data"]["rejection_reasons"] == {"missing_search_api_key": 1}


@pytest.mark.anyio
async def test_jobs_recent_returns_lead_generation_stats() -> None:
    db = build_memory_session()
    auth = await AuthService(db).signup(
        tenant_id="tenant-job-stats",
        tenant_name="Tenant Job Stats",
        tenant_slug="tenant-job-stats",
        email="owner@jobstats.test",
        password="secret123",
        full_name="Owner",
        plan="Agency",
    )
    tenant = TenantContext(tenant_id=auth.tenant_id)
    db.for_tenant(tenant).save(
        "jobs",
        Job(
            tenant_id=auth.tenant_id,
            name="lead_generation",
            status="completed",
            result={
                "status": "SUCCESS",
                "message": "Lead generation completed with 0 leads: no URLs were discovered.",
                "data": {
                    "raw_results_count": 10,
                    "filtered_results_count": 0,
                    "saved_leads": 0,
                    "lead_count": 0,
                },
            },
        ),
    )
    app = create_fastapi_app(db=db)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/jobs/recent", headers=_auth_headers(auth.token))

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["result"]["raw_results_count"] == 10
    assert item["result"]["saved_leads"] == 0


@pytest.mark.anyio
async def test_job_events_endpoint_is_tenant_scoped() -> None:
    db = build_memory_session()
    owner = await AuthService(db).signup(
        tenant_id="tenant-events-owner",
        tenant_name="Tenant Events Owner",
        tenant_slug="tenant-events-owner",
        email="owner@events.test",
        password="secret123",
        full_name="Owner",
        plan="Agency",
    )
    other = await AuthService(db).signup(
        tenant_id="tenant-events-other",
        tenant_name="Tenant Events Other",
        tenant_slug="tenant-events-other",
        email="other@events.test",
        password="secret123",
        full_name="Other",
        plan="Agency",
    )
    tenant = TenantContext(tenant_id=owner.tenant_id)
    job = db.for_tenant(tenant).save(
        "jobs",
        Job(
            tenant_id=owner.tenant_id,
            name="lead_generation",
            status="completed",
            result_summary={
                "progress_percentage": 100,
                "current_stage": "completed",
                "stats": {"saved_leads": 1, "rejected_leads_count": 2},
                "events": [{"stage": "saving", "status": "saved", "domain": "acme.test", "message": "lead_saved"}],
            },
        ),
    )
    app = create_fastapi_app(db=db)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        ok = await client.get(f"/jobs/{job.id}/events", headers=_auth_headers(owner.token))
        status = await client.get(f"/jobs/{job.id}/status", headers=_auth_headers(owner.token))
        missing = await client.get(f"/jobs/{job.id}/events", headers=_auth_headers(other.token))

    assert ok.status_code == 200
    assert ok.json()["events"][0]["message"] == "lead_saved"
    assert status.json()["lead_stats"]["saved_leads"] == 1
    assert missing.status_code == 404


def test_scraper_uses_separate_contexts_for_contact_and_content(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_contact_info(website: str, context=None):
        assert context is not None
        context.visited_urls.add(website)
        context.last_status = "no_email"
        return {"company_name": "Acme", "email": "info@acme.test"}

    def fake_scrape_website(website: str, context=None):
        assert context is not None
        assert website not in context.visited_urls
        context.last_status = "ok"
        return "Acme builds industrial automation systems.", "requests_bs4", ""

    monkeypatch.setattr(legacy_leads, "extract_contact_info", fake_contact_info)
    monkeypatch.setattr(legacy_leads, "scrape_website", fake_scrape_website)

    result = ScraperAgent().run({"website": "https://acme.test"})

    assert result["website_text"]
    assert result["scrape_status"] == "ok"
    assert result["contact_status"] == "no_email"


def test_scoring_agent_falls_back_when_claude_key_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(
        legacy_leads,
        "analyze_lead_with_claude",
        lambda website_text: pytest.fail("Claude should not be called without ANTHROPIC_API_KEY."),
    )

    result = ScoringAgent().run(
        {
            "website": "https://retail.example",
            "company_name": "Retail Co",
            "email": "info@retail.example",
            "website_text": "Retail Co runs online stores and needs inventory automation.",
            "query": "retail ecommerce companies",
            "lead_status": "Pending",
        }
    )

    lead = result["lead"]
    assert lead["ai_mode"] == "fallback"
    assert lead["industry"] == "Retail & Ecommerce"
    assert "retail" in lead["analysis_reason"].lower()


def test_contact_extraction_reads_mailto_footer_structured_and_careers(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = {
        "https://acme.test": {
            "status": "SUCCESS",
            "content": """
                <html>
                  <head>
                    <script type="application/ld+json">
                      {"@type": "Organization", "email": "support@acme.test", "telephone": "+1 555 123 4567"}
                    </script>
                  </head>
                  <body>
                    <a href="/careers">Careers</a>
                    <footer>Footer contact: info@acme.test</footer>
                  </body>
                </html>
            """,
            "method_used": "fake",
        },
        "https://acme.test/careers": {
            "status": "SUCCESS",
            "content": '<html><body><a href="mailto:sales@acme.test">Email sales</a></body></html>',
            "method_used": "fake",
        },
    }

    def fake_fetch_page(url: str, context=None):
        return pages.get(url.rstrip("/"), {"status": "FAILED", "content": "", "failure_reason": "missing"})

    monkeypatch.setattr(legacy_leads, "fetch_page", fake_fetch_page)

    contact = legacy_leads.extract_contact_info("https://acme.test")

    assert contact["email"] == "sales@acme.test"
    assert contact["phone"] == "+1 555 123 4567"
    assert contact["email_confidence"] == "verified_email"
    assert contact["lead_readiness_score"] == 100
    sources = {item["source"] for item in contact["email_candidates"]}
    assert {"mailto", "footer", "structured_data"}.issubset(sources)


def test_contact_extraction_adds_likely_common_email_when_no_observed_email(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = {
        "https://quietco.test": {
            "status": "SUCCESS",
            "content": '<html><body><a href="/contact">Contact</a><p>Call us for service.</p></body></html>',
            "method_used": "fake",
        },
        "https://quietco.test/contact": {
            "status": "SUCCESS",
            "content": "<html><body><p>We reply during business hours.</p></body></html>",
            "method_used": "fake",
        },
    }

    def fake_fetch_page(url: str, context=None):
        return pages.get(url.rstrip("/"), {"status": "FAILED", "content": "", "failure_reason": "missing"})

    monkeypatch.setattr(legacy_leads, "fetch_page", fake_fetch_page)

    contact = legacy_leads.extract_contact_info("https://quietco.test")

    assert contact["email"] == ""
    assert contact["likely_email"] == "info@quietco.test"
    assert contact["likely_emails"] == [
        "info@quietco.test",
        "sales@quietco.test",
        "contact@quietco.test",
        "hello@quietco.test",
        "support@quietco.test",
    ]
    assert contact["email_confidence"] == "likely_email"
    assert contact["lead_readiness_score"] == 70


@pytest.mark.anyio
async def test_api_exports_only_standardized_lead_fields() -> None:
    app = create_fastapi_app(db=build_memory_session())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        auth = (
            await client.post(
                "/signup",
                json={
                    "tenant_id": "tenant-output",
                    "tenant_name": "Tenant Output",
                    "tenant_slug": "tenant-output",
                    "email": "owner@tenant-output.test",
                    "password": "secret123",
                    "full_name": "Owner Output",
                },
            )
        ).json()
        headers = _auth_headers(auth["token"])

        create = await client.post(
            "/leads",
            headers=headers,
            json={
                "company": "Acme Industrial",
                "email": "info@acme.test",
                "website": "https://acme.test",
                "score": 75,
                "service_reason": "Strong business fit",
            },
        )
        assert create.status_code == 200
        assert create.json()["email"] == "info@acme.test"

        listing = await client.get("/leads", headers=headers)
        row = listing.json()["items"][0]
        assert list(row.keys()) == [
            "company_url",
            "country",
            "verified_email",
            "phone",
            "likely_email",
            "email_confidence",
            "lead_readiness_score",
            "service_reason",
            "industry",
            "score",
            "outreach_status",
            "outreach_error",
            "followup_count",
            "reply_status",
            "last_reply_at",
            "email_quality",
            "lead_quality_grade",
            "save_reason",
        ]
        assert row["company_url"] == "https://acme.test"
        assert row["country"] == ""
        assert row["verified_email"] == "info@acme.test"
        assert row["phone"] == ""
        assert row["likely_email"] == ""
        assert row["email_confidence"] == "verified_email"
        assert row["lead_readiness_score"] == 100
        assert row["service_reason"] == "Strong business fit"


@pytest.mark.anyio
async def test_country_snippet_is_saved_as_empty() -> None:
    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-country-empty")
    lead = Lead(
        tenant_id=tenant.tenant_id,
        company_url="https://acme.test",
        verified_email="info@acme.test",
        country="Moodle LMS setup and customization for training teams",
        service_reason="Uses online learning systems",
    )

    saved = await LeadService(db).upsert_lead(tenant, lead)

    assert saved.country == ""


@pytest.mark.anyio
async def test_known_countries_are_normalized() -> None:
    db = build_memory_session()
    service = LeadService(db)
    tenant = TenantContext(tenant_id="tenant-country-valid")

    cases = [
        ("pakistan", "Pakistan"),
        ("Kingdom of Saudi Arabia", "Saudi Arabia"),
        ("United Arab Emirates", "UAE"),
    ]
    for index, (raw_country, expected) in enumerate(cases):
        saved = await service.upsert_lead(
            tenant,
            Lead(
                tenant_id=tenant.tenant_id,
                company_url=f"https://company-{index}.test",
                verified_email=f"lead-{index}@company.test",
                country=raw_country,
            ),
        )
        assert saved.country == expected


@pytest.mark.anyio
async def test_score_breakdown_is_not_service_reason() -> None:
    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-reason-empty")
    saved = await LeadService(db).upsert_lead(
        tenant,
        Lead(
            tenant_id=tenant.tenant_id,
            company_url="https://breakdown.test",
            verified_email="info@breakdown.test",
            service_reason="email=40/40 | phone=25/25 | relevance=0/20 | quality=10/10",
        ),
    )

    assert saved.service_reason == "This appears to be a relevant business with public contact details."


@pytest.mark.anyio
async def test_lead_reason_fallback_is_saved_when_data_is_weak() -> None:
    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-lead-reason-fallback")
    saved = await LeadService(db).upsert_lead(
        tenant,
        Lead(tenant_id=tenant.tenant_id, company_url="https://weak-reason.test"),
    )

    assert saved.service_reason == "This appears to be a relevant business with public contact details."
    assert "search query" not in saved.service_reason.lower()


@pytest.mark.anyio
async def test_lead_reason_sanitizes_forbidden_generation_words() -> None:
    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-lead-reason-clean")
    saved = await LeadService(db).upsert_lead(
        tenant,
        Lead(
            tenant_id=tenant.tenant_id,
            company_url="https://clean-reason.test",
            service_reason="Clean Co matches the search query and may need workflow automation.",
        ),
    )

    assert "search query" not in saved.service_reason.lower()
    assert "appears to be relevant" in saved.service_reason


@pytest.mark.anyio
async def test_upsert_clears_existing_score_breakdown_service_reason() -> None:
    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-clear-score")
    scoped = db.for_tenant(tenant)
    stale = scoped.save(
        "leads",
        Lead(
            tenant_id=tenant.tenant_id,
            company_url="https://clear-score.test",
            verified_email="info@clear-score.test",
            email="info@clear-score.test",
            service_reason="email=40/40 | phone=25/25 | relevance=0/20",
        ),
    )

    saved = await LeadService(db).upsert_lead(
        tenant,
        Lead(
            tenant_id=tenant.tenant_id,
            company_url="https://clear-score.test",
            verified_email="info@clear-score.test",
            service_reason="email=40/40 | phone=25/25",
        ),
    )

    assert saved.id == stale.id
    assert saved.service_reason == "This appears to be a relevant business with public contact details."


@pytest.mark.anyio
async def test_upsert_clears_existing_rejected_verified_email() -> None:
    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-clear-email")
    scoped = db.for_tenant(tenant)
    stale = scoped.save(
        "leads",
        Lead(
            tenant_id=tenant.tenant_id,
            company_url="https://energycities.org",
            verified_email="info@pacificenergy.com.au",
            email="info@pacificenergy.com.au",
        ),
    )

    saved = await LeadService(db).upsert_lead(
        tenant,
        Lead(
            tenant_id=tenant.tenant_id,
            company_url="https://energycities.org",
            verified_email="info@pacificenergy.com.au",
        ),
    )

    assert saved.id == stale.id
    assert saved.verified_email == ""
    assert saved.email == ""
    assert saved.metadata["rejected_emails"] == [
        {"email": "info@pacificenergy.com.au", "reason": "domain_mismatch"}
    ]


@pytest.mark.anyio
async def test_upsert_dedupes_by_company_url_when_email_is_blank() -> None:
    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-url-dedupe")
    service = LeadService(db)

    first = await service.upsert_lead(
        tenant,
        Lead(tenant_id=tenant.tenant_id, company_url="https://dedupe.test", service_reason="Initial reason"),
    )
    second = await service.upsert_lead(
        tenant,
        Lead(tenant_id=tenant.tenant_id, company_url="https://dedupe.test", service_reason="Updated reason"),
    )

    leads = db.for_tenant(tenant).list("leads")
    assert second.id == first.id
    assert len(leads) == 1
    assert leads[0].service_reason == "Updated reason"


@pytest.mark.anyio
async def test_same_domain_generic_email_is_kept_with_verified_confidence() -> None:
    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-email-generic")
    saved = await LeadService(db).upsert_lead(
        tenant,
        Lead(
            tenant_id=tenant.tenant_id,
            company_url="https://company.com",
            verified_email="info@company.com",
        ),
    )

    assert saved.verified_email == "info@company.com"
    assert saved.metadata["email_quality"] == "generic"
    assert saved.metadata["email_confidence"] == "verified_email"
    assert saved.metadata["lead_readiness_score"] == 100


@pytest.mark.anyio
async def test_same_domain_personal_email_beats_generic() -> None:
    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-email-rank")
    saved = await LeadService(db).upsert_lead(
        tenant,
        Lead(
            tenant_id=tenant.tenant_id,
            company_url="https://alraee.com.sa",
            verified_email="info@alraee.com.sa",
            metadata={"candidate_emails": ["info@alraee.com.sa", "bilal@alraee.com.sa"]},
        ),
    )

    assert saved.verified_email == "bilal@alraee.com.sa"
    assert saved.metadata["email_quality"] == "direct"
    assert saved.metadata["email_confidence"] == "verified_email"
    assert saved.metadata["lead_readiness_score"] == 100
    assert saved.metadata["rejected_emails"] == [
        {"email": "info@alraee.com.sa", "reason": "lower_ranked_same_domain_candidate"}
    ]


@pytest.mark.anyio
async def test_mismatched_domain_email_is_rejected() -> None:
    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-email-mismatch")
    saved = await LeadService(db).upsert_lead(
        tenant,
        Lead(
            tenant_id=tenant.tenant_id,
            company_url="https://energycities.org",
            verified_email="info@pacificenergy.com.au",
        ),
    )

    assert saved.verified_email == ""
    assert saved.email == ""
    assert saved.metadata["email_quality"] == "missing"
    assert saved.metadata["email_confidence"] == "unknown"
    assert saved.metadata["lead_readiness_score"] == 0
    assert saved.metadata["rejected_emails"] == [
        {"email": "info@pacificenergy.com.au", "reason": "domain_mismatch"}
    ]


@pytest.mark.anyio
async def test_public_email_domain_is_rejected() -> None:
    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-email-public")
    saved = await LeadService(db).upsert_lead(
        tenant,
        Lead(
            tenant_id=tenant.tenant_id,
            company_url="https://company.com",
            verified_email="owner@gmail.com",
        ),
    )

    assert saved.verified_email == ""
    assert saved.metadata["rejected_emails"] == [
        {"email": "owner@gmail.com", "reason": "public_email_domain"}
    ]


@pytest.mark.anyio
async def test_likely_email_sets_readiness_without_verified_email() -> None:
    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-likely-email")

    saved = await LeadService(db).upsert_lead(
        tenant,
        Lead(
            tenant_id=tenant.tenant_id,
            company_url="https://likely.test",
            metadata={"likely_email": "info@likely.test", "email_confidence": "likely_email"},
        ),
    )

    assert saved.verified_email == ""
    assert saved.email == ""
    assert saved.metadata["likely_email"] == "info@likely.test"
    assert saved.metadata["email_quality"] == "likely"
    assert saved.metadata["email_confidence"] == "likely_email"
    assert saved.metadata["lead_readiness_score"] == 70


@pytest.mark.anyio
async def test_dashboard_snapshot_reports_contact_audit_counts() -> None:
    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-contact-audit")
    service = LeadService(db)

    await service.upsert_lead(
        tenant,
        Lead(
            tenant_id=tenant.tenant_id,
            company_url="https://ready.test",
            verified_email="info@ready.test",
            email="info@ready.test",
        ),
    )
    await service.upsert_lead(
        tenant,
        Lead(
            tenant_id=tenant.tenant_id,
            company_url="https://phone.test",
            phone="+923000000000",
        ),
    )
    await service.upsert_lead(
        tenant,
        Lead(
            tenant_id=tenant.tenant_id,
            company_url="https://likely.test",
            metadata={"likely_email": "info@likely.test"},
        ),
    )
    await service.upsert_lead(
        tenant,
        Lead(
            tenant_id=tenant.tenant_id,
            company_url="https://empty.test",
        ),
    )

    snapshot = await service.dashboard_snapshot(tenant)

    assert snapshot["lead_count"] == 4
    assert snapshot["leads_with_website"] == 4
    assert snapshot["leads_with_phone"] == 1
    assert snapshot["leads_with_email"] == 1
    assert snapshot["leads_with_verified_email"] == 1
    assert snapshot["verified_email_rate"] == 25.0
    assert snapshot["email_ready_leads"] == 1
    assert snapshot["likely_email_leads"] == 1
    assert snapshot["phone_only_leads"] == 1
    assert snapshot["no_contact_leads"] == 1


@pytest.mark.anyio
async def test_suspicious_short_email_domain_is_rejected() -> None:
    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-email-suspicious")
    saved = await LeadService(db).upsert_lead(
        tenant,
        Lead(
            tenant_id=tenant.tenant_id,
            company_url="https://mustakbil.com",
            verified_email="jobs@m.al",
        ),
    )

    assert saved.verified_email == ""
    assert saved.metadata["rejected_emails"] == [
        {"email": "jobs@m.al", "reason": "suspicious_email_domain"}
    ]


@pytest.mark.anyio
async def test_lead_generation_uses_claude_reason_and_keeps_score_breakdown_in_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-generation-reason")
    score_breakdown = "email=40/40 | phone=25/25 | relevance=0/20 | quality=10/10"

    monkeypatch.setattr(legacy_leads, "load_environment", lambda: None)
    monkeypatch.setattr(
        legacy_leads,
        "process_query",
        lambda query, seen_websites, limit, ai_mode="": [
            {
                "qualified": True,
                "decision": "accepted",
                "company_name": "Reason Co",
                "website": "https://reason.test",
                "email": "info@reason.test",
                "phone": "+971 55 000 0000",
                "address": "About Us Years of Experience building teams",
                "country": "",
                "industry": "Technology",
                "lead_score": 75,
                "reason": score_breakdown,
                "analysis_reason": "The company shows demand for business workflow automation.",
                "intent_summary": "Needs operational automation",
                "email_status": "pending",
            }
        ],
    )

    result = await LeadGenerationAgent().run(
        AgentRequest(tenant=tenant, payload={"query": "automation companies", "limit": 1}),
        db,
    )

    saved = db.for_tenant(tenant).list("leads")[0]
    assert result["status"] == "SUCCESS"
    assert saved.country == ""
    assert saved.service_reason == "The company shows demand for business workflow automation."
    assert "email=40/40" not in saved.service_reason
    assert "phone=25/25" not in saved.service_reason
    assert saved.metadata["score_breakdown"] == score_breakdown


@pytest.mark.anyio
async def test_free_plan_lead_generation_uses_fallback_without_anthropic_key(monkeypatch: pytest.MonkeyPatch) -> None:
    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-free-fallback")
    db.tenants.save(Tenant(tenant_id=tenant.tenant_id, name="Tenant", slug="tenant-free-fallback", subscription_plan="Free"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(legacy_leads, "load_environment", lambda: None)
    monkeypatch.setattr(legacy_leads, "search_google", lambda query: [{"link": "https://logistics.example"}])
    monkeypatch.setattr(legacy_leads, "extract_websites", lambda results: ["https://logistics.example"])
    monkeypatch.setattr(
        legacy_leads,
        "extract_contact_info",
        lambda website, context=None: {
            "company_name": "Logistics Co",
            "email": "info@logistics.example",
            "phone": "",
            "address": "",
            "contact_page": "https://logistics.example/contact",
        },
    )
    monkeypatch.setattr(
        legacy_leads,
        "scrape_website",
        lambda website, context=None: (
            "Logistics Co provides freight, warehouse, shipping, managed services and automation support.",
            "requests_bs4",
            "",
        ),
    )
    monkeypatch.setattr(
        legacy_leads,
        "analyze_lead_with_claude",
        lambda website_text: pytest.fail("Claude should not be called for Free fallback lead generation."),
    )

    result = await LeadGenerationAgent().run(
        AgentRequest(tenant=tenant, payload={"query": "logistics companies in UAE", "limit": 1}),
        db,
    )

    saved = db.for_tenant(tenant).list("leads")[0]
    assert result["status"] == "SUCCESS"
    assert saved.industry == "Logistics"
    assert "logistics" in saved.service_reason.lower()
    assert saved.metadata["ai_mode"] == "fallback"


@pytest.mark.anyio
async def test_lead_generation_persists_country_from_query_when_raw_country_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-query-country")

    monkeypatch.setattr(legacy_leads, "load_environment", lambda: None)
    monkeypatch.setattr(
        legacy_leads,
        "process_query",
        lambda query, seen_websites, limit, ai_mode="": [
            {
                "qualified": True,
                "decision": "accepted",
                "company_name": "Clothing Co",
                "website": "https://clothing.example",
                "email": "sales@clothing.example",
                "country": "",
                "industry": "Apparel & Fashion",
                "lead_score": 80,
                "analysis_reason": "The company sells apparel online and may need commerce automation.",
                "email_status": "pending",
            }
        ],
    )

    await LeadGenerationAgent().run(
        AgentRequest(tenant=tenant, payload={"query": "clothing companies in Pakistan", "limit": 1}),
        db,
    )

    saved = db.for_tenant(tenant).list("leads")[0]
    assert saved.country == "Pakistan"


@pytest.mark.anyio
async def test_lead_generation_uses_intent_summary_when_analysis_reason_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-intent-summary")

    monkeypatch.setattr(legacy_leads, "load_environment", lambda: None)
    monkeypatch.setattr(
        legacy_leads,
        "process_query",
        lambda query, seen_websites, limit, ai_mode="": [
            {
                "qualified": True,
                "decision": "accepted",
                "company_name": "Ops Co",
                "website": "https://ops.example",
                "email": "hello@ops.example",
                "industry": "Operations Technology",
                "lead_score": 80,
                "analysis_reason": "",
                "intent_summary": "The company shows operational complexity that may require automation support.",
                "email_status": "pending",
            }
        ],
    )

    await LeadGenerationAgent().run(
        AgentRequest(tenant=tenant, payload={"query": "operations companies in UAE", "limit": 1}),
        db,
    )

    saved = db.for_tenant(tenant).list("leads")[0]
    assert saved.service_reason == "The company shows operational complexity that may require automation support."
    assert saved.country == "UAE"


@pytest.mark.anyio
async def test_valid_claude_industry_is_preserved_when_not_in_local_map() -> None:
    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-industry-preserve")
    saved = await LeadService(db).upsert_lead(
        tenant,
        Lead(
            tenant_id=tenant.tenant_id,
            company_url="https://fashion.example",
            verified_email="info@fashion.example",
            industry="Apparel & Fashion",
        ),
    )

    assert saved.industry == "Apparel & Fashion"


@pytest.mark.anyio
async def test_missing_claude_industry_remains_other_and_reason_fallback(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-missing-claude")

    monkeypatch.setattr(legacy_leads, "load_environment", lambda: None)
    monkeypatch.setattr(
        legacy_leads,
        "process_query",
        lambda query, seen_websites, limit, ai_mode="": [
            {
                "qualified": True,
                "decision": "accepted",
                "company_name": "No Claude Co",
                "website": "https://noclaude.example",
                "email": "info@noclaude.example",
                "industry": "",
                "lead_score": 80,
                "analysis_reason": "",
                "intent_summary": "",
                "email_status": "pending",
            }
        ],
    )

    with caplog.at_level("INFO"):
        await LeadGenerationAgent().run(
            AgentRequest(tenant=tenant, payload={"query": "companies in Saudi Arabia", "limit": 1}),
            db,
        )

    saved = db.for_tenant(tenant).list("leads")[0]
    assert saved.service_reason == "This appears to be a relevant business with public contact details."
    assert saved.industry == "Other"
    assert saved.country == "Saudi Arabia"
    assert "missing_claude_reason_or_intent_summary" in caplog.text
    assert "missing_claude_industry" in caplog.text


@pytest.mark.anyio
async def test_lead_generation_rejects_directory_job_board_and_data_broker_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-junk-sources")

    monkeypatch.setattr(legacy_leads, "load_environment", lambda: None)
    monkeypatch.setattr(
        legacy_leads,
        "process_query",
        lambda query, seen_websites, limit, ai_mode="": [
            {
                "qualified": True,
                "decision": "accepted",
                "company_name": "The Saudi Directory",
                "website": "https://the-saudi.net",
                "email": "info@the-saudi.net",
                "industry": "Business Directory",
                "lead_score": 90,
                "analysis_reason": "Directory result",
                "email_status": "pending",
            },
            {
                "qualified": True,
                "decision": "accepted",
                "company_name": "Mustakbil",
                "website": "https://sa.mustakbil.com",
                "email": "jobs@mustakbil.com",
                "industry": "Employment Platform",
                "lead_score": 90,
                "analysis_reason": "Job board result",
                "email_status": "pending",
            },
            {
                "qualified": True,
                "decision": "accepted",
                "company_name": "Reach Gulf Business",
                "website": "https://www.reachgulfbusiness.com",
                "email": "support@reachgulfbusiness.com",
                "industry": "Email List Provider",
                "lead_score": 90,
                "analysis_reason": "Data broker result",
                "email_status": "pending",
            },
        ],
    )

    result = await LeadGenerationAgent().run(
        AgentRequest(tenant=tenant, payload={"query": "Saudi Arabia companies", "limit": 3}),
        db,
    )

    assert result["status"] == "SUCCESS"
    assert result["data"]["saved_leads"] == 0
    assert result["data"]["skipped_leads"] == 3
    assert db.for_tenant(tenant).list("leads") == []


class _FakeProvider:
    def __init__(self) -> None:
        self.sent = []

    async def send(self, account, request):
        self.sent.append((account.tenant_id, request.to, request.subject, request.body))
        return ProviderSendResult(message_id="msg-1", thread_id="thread-1", raw={"id": "msg-1"})

    async def fetch_replies(self, account, cursor: str = ""):
        return []


@pytest.mark.anyio
async def test_outreach_agent_uses_tenant_gmail_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-outreach")
    monkeypatch.setattr("app.configs.settings.settings.secret_encryption_key", "test-key")
    db.tenants.save(Tenant(tenant_id=tenant.tenant_id, name="Tenant", slug="tenant-outreach", subscription_plan="Pro"))
    await ProviderCredentialService(db).save_gmail_credentials(
        tenant,
        {
            "client_id": "client",
            "client_secret": "secret",
            "refresh_token": "refresh-token",
            "access_token": "access-token",
            "email_address": "sender@tenant.test",
            "scopes": ["gmail.send"],
        },
    )
    db.for_tenant(tenant).save(
        "leads",
        Lead(
            tenant_id=tenant.tenant_id,
            company="Acme",
            website="https://acme.test",
            verified_email="info@acme.test",
            email="info@acme.test",
            reason="Good fit",
            score=80,
            status="pending",
        ),
    )
    fake_provider = _FakeProvider()
    monkeypatch.setattr(outreach_module, "build_provider_registry", lambda: {"gmail": fake_provider})

    result = await OutreachAgent().run(AgentRequest(tenant=tenant, payload={}), db)

    assert result["sent_messages"] == 1
    assert fake_provider.sent[0][0] == tenant.tenant_id
    assert fake_provider.sent[0][1] == "info@acme.test"
    assert fake_provider.sent[0][2]
    assert "unsubscribe" in fake_provider.sent[0][3].lower()
