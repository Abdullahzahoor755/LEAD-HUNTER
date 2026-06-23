from __future__ import annotations

import httpx
import pytest
from contextlib import asynccontextmanager

import app.agents.outreach as outreach_module
import leads as legacy_leads
from app.agents.base import AgentRequest
from app.agents.lead_generation import LeadGenerationAgent
from app.agents.lead_pipeline import DiscoveryAgent, ScoringAgent, ScraperAgent
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


def test_dentist_query_builds_contact_focused_variants() -> None:
    assert legacy_leads.build_search_query_variants(query="dentist clinics in Lahore") == [
        "dentists in Lahore contact",
        "dental clinics in Lahore phone",
        "best dental clinic Lahore contact",
        "dentist Lahore email phone",
    ]


def test_discovery_requests_larger_raw_pool_for_contact_search(monkeypatch: pytest.MonkeyPatch) -> None:
    requested_sizes: list[int] = []

    def fake_search(query: str, num: int = 10):
        requested_sizes.append(num)
        return []

    monkeypatch.setattr(legacy_leads, "search_google", fake_search)

    result = DiscoveryAgent().run({"query": "dentist clinics in Lahore", "limit": 5})

    assert len(result["query_variants"]) == 4
    assert requested_sizes == [30, 30, 30, 30]


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
    assert grade == "B"


def test_beta_acceptance_allows_low_score_verified_email_lead() -> None:
    lead = {
        "website": "https://low-score-email.test",
        "company_name": "Low Score Email Co",
        "email": "info@low-score-email.test",
        "phone": "",
        "lead_score": 10,
        "domain_type": "business",
        "relevance_passed": True,
    }

    assert legacy_leads.is_qualified_lead(lead) is True
    assert legacy_leads.beta_lead_readiness(lead) == "email_ready"


def test_beta_acceptance_allows_phone_ready_lead_without_email() -> None:
    lead = {
        "website": "https://phone-ready.test",
        "company_name": "Phone Ready Co",
        "email": "",
        "phone": "+966 55 123 4567",
        "lead_score": 10,
        "domain_type": "business",
        "relevance_passed": True,
    }

    assert legacy_leads.is_qualified_lead(lead) is True
    assert legacy_leads.beta_lead_readiness(lead) == "phone_ready"


def test_research_needed_business_domain_without_contact_is_not_qualified() -> None:
    lead = {
        "website": "https://research-needed.test",
        "company_name": "Research Needed Co",
        "email": "",
        "phone": "",
        "lead_score": 5,
        "domain_type": "business",
    }

    assert legacy_leads.is_qualified_lead(lead) is False
    assert legacy_leads.beta_lead_readiness(lead) == "research_needed"


def test_explicit_research_needed_is_not_overridden_by_generic_email() -> None:
    lead = {
        "website": "https://generic-email.test",
        "company_name": "Generic Email Co",
        "email": "info@generic-email.test",
        "phone": "",
        "domain_type": "business",
        "readiness": "research_needed",
        "email_channel_eligible": False,
    }

    assert legacy_leads.is_qualified_lead(lead) is False


@pytest.mark.parametrize(
    ("lead", "expected"),
    [
        (
            {
                "website": "https://forbes-style.example",
                "company_name": "Forbes Style Publisher",
                "email": "editor@forbes-style.example",
                "domain_type": "business",
                "relevance_passed": False,
            },
            False,
        ),
        (
            {
                "website": "https://pakistan-embassy.example",
                "company_name": "Pakistan Embassy",
                "email": "office@pakistan-embassy.example",
                "domain_type": "neutral",
                "relevance_passed": False,
            },
            False,
        ),
        (
            {
                "website": "https://directory.example/listings",
                "company_name": "Business Directory",
                "email": "sales@directory.example",
                "domain_type": "listing",
                "is_directory": True,
                "relevance_passed": True,
            },
            False,
        ),
        (
            {
                "website": "https://energy-network.example",
                "company_name": "Nonprofit Energy Network",
                "phone": "+923001234567",
                "domain_type": "neutral",
                "relevance_passed": False,
            },
            False,
        ),
        (
            {
                "website": "https://real-phone-company.example",
                "company_name": "Real Phone Company",
                "phone": "0300 1234567",
                "domain_type": "business",
                "relevance_passed": True,
            },
            True,
        ),
        (
            {
                "website": "https://real-email-company.example",
                "company_name": "Real Email Company",
                "email": "owner@real-email-company.example",
                "domain_type": "business",
                "relevance_passed": True,
            },
            True,
        ),
    ],
)
def test_qualification_requires_relevance_and_contact(lead: dict, expected: bool) -> None:
    assert legacy_leads.is_qualified_lead(lead) is expected


def test_fallback_analysis_is_conservative_and_uses_website_content() -> None:
    analysis = ScoringAgent()._fallback_analysis(
        query="AI software companies in Pakistan",
        website_text="A dental clinic providing patient healthcare and treatment services.",
        company_name="Example Dental Clinic",
    )

    assert analysis["industry"] == "Healthcare"
    assert analysis["needs_it_services"] is True
    assert analysis["relevance_passed"] is False
    assert analysis["intent_analysis"] == {
        "buying_intent_score": 0,
        "service_demand_score": 0,
        "urgency_score": 0,
        "intent_summary": analysis["reason"],
        "signals": [],
    }


def test_demo_mode_accepts_real_scraped_email(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEADGEN_DEMO_MODE", "true")
    lead = ScoringAgent().run(
        {
            "website": "https://demo-email.example",
            "company_name": "Demo Email Company",
            "email": "owner@demo-email.example",
            "website_text": "Operating business providing professional services. " * 10,
            "ai_mode": "fallback",
            "scrape_status": "ok",
        }
    )["lead"]

    assert lead["qualified"] is True
    assert lead["demo_accepted_contact"] is True
    assert lead["email"] == "owner@demo-email.example"
    assert lead["reason"] == "Website has scraped business contact details."


def test_demo_mode_accepts_real_scraped_phone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEADGEN_DEMO_MODE", "true")
    lead = ScoringAgent().run(
        {
            "website": "https://demo-phone.example",
            "company_name": "Demo Phone Company",
            "phone": "0300 1234567",
            "contact_page": "https://demo-phone.example/contact",
            "website_text": "Operating business with a public contact page. " * 10,
            "ai_mode": "fallback",
            "scrape_status": "ok",
        }
    )["lead"]

    assert lead["qualified"] is True
    assert lead["phone"] == "+923001234567"
    assert lead["reason"] == "Company has a public phone number and contact page."


def test_demo_mode_rejects_guessed_email_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEADGEN_DEMO_MODE", "true")
    lead = ScoringAgent().run(
        {
            "website": "https://demo-guessed.example",
            "company_name": "Demo Guessed Company",
            "likely_email": "info@demo-guessed.example",
            "website_text": "Operating business without published contact details. " * 10,
            "ai_mode": "fallback",
            "scrape_status": "ok",
        }
    )["lead"]

    assert lead["qualified"] is False
    assert lead["skip_reason"] == "no_contact"


@pytest.mark.parametrize(
    ("website", "company_name", "website_text"),
    [
        ("https://directory.example/listings", "Business Directory", "Directory listings for many companies. " * 10),
        ("https://publisher.example", "Industry Magazine Publisher", "News publisher and magazine articles. " * 10),
        ("https://services.gov", "Government Ministry", "Official government ministry services. " * 10),
    ],
)
def test_demo_mode_rejects_excluded_domains(
    monkeypatch: pytest.MonkeyPatch,
    website: str,
    company_name: str,
    website_text: str,
) -> None:
    monkeypatch.setenv("LEADGEN_DEMO_MODE", "true")
    lead = ScoringAgent().run(
        {
            "website": website,
            "company_name": company_name,
            "email": "contact@company.example",
            "website_text": website_text,
            "ai_mode": "fallback",
            "scrape_status": "ok",
        }
    )["lead"]

    assert lead["qualified"] is False
    assert lead["skip_reason"] in ("junk_source", "excluded_domain", "directory_or_listing")


def test_production_mode_accepts_qualified_lead(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEADGEN_DEMO_MODE", "false")
    lead = ScoringAgent().run(
        {
            "website": "https://strict-production.example",
            "company_name": "Strict Production Company",
            "email": "owner@strict-production.example",
            "website_text": "Operating business providing professional services. " * 10,
            "ai_mode": "fallback",
            "scrape_status": "ok",
        }
    )["lead"]

    assert lead["qualified"] is True
    assert lead["relevance_passed"] is True
    assert lead["readiness"] in ("email_ready", "phone_ready")


@pytest.mark.parametrize(
    ("raw_phone", "expected"),
    [
        ("0300 1234567", "+923001234567"),
        ("0300-1234567", "+923001234567"),
        ("0092 300 1234567", "+923001234567"),
        ("021-12345678", "+922112345678"),
        ("042-1234567", "+92421234567"),
    ],
)
def test_pakistan_phone_formats_are_normalized(raw_phone: str, expected: str) -> None:
    assert legacy_leads.normalize_phone(raw_phone) == expected
    assert LeadService.normalize_phone(raw_phone) == expected


def test_beta_acceptance_still_rejects_directory_invalid_and_aggregator_domains() -> None:
    assert legacy_leads.is_qualified_lead(
        {
            "website": "https://example.test/directory/software-companies",
            "company_name": "Example Directory",
            "email": "info@example.test",
            "phone": "",
            "lead_score": 95,
            "domain_type": "listing",
            "is_directory": True,
            "relevance_passed": True,
        }
    ) is False
    assert legacy_leads.is_qualified_lead(
        {
            "website": "not a url",
            "company_name": "Invalid URL Co",
            "email": "info@invalid.test",
            "phone": "",
            "lead_score": 95,
            "relevance_passed": True,
        }
    ) is False
    assert legacy_leads.is_qualified_lead(
        {
            "website": "https://clutch.co/sa/developers",
            "company_name": "Clutch",
            "email": "info@clutch.co",
            "phone": "",
            "lead_score": 95,
            "domain_type": "business",
            "relevance_passed": True,
        }
    ) is False


def test_duplicate_domain_filter_still_rejects_repeated_domains() -> None:
    _, stats = legacy_leads.extract_websites_with_stats(
        [
            {"title": "Alpha Software", "link": "https://alpha.test", "snippet": "Software company"},
            {"title": "Alpha Software Contact", "link": "https://www.alpha.test/contact", "snippet": "Software company"},
        ]
    )

    assert stats["filtered_results_count"] == 1
    assert stats["duplicate_domain_count"] == 1


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


def test_scraper_classifies_successful_page_without_text_as_empty_text(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_contact_info(website: str, context=None):
        return {"company_name": "Empty Co", "email": "person@empty.test", "phone": "+923001234567"}

    def fake_scrape_website(website: str, context=None):
        context.last_status = "empty_text"
        context.metrics["pages_success_text_empty"] = 1
        return "", "fallback_failed", "FAILED_EMPTY_CONTENT"

    monkeypatch.setattr(legacy_leads, "extract_contact_info", fake_contact_info)
    monkeypatch.setattr(legacy_leads, "scrape_website", fake_scrape_website)

    result = ScraperAgent().run({"website": "https://empty.test"})

    assert result["scrape_status"] == "empty_text"
    assert result["contact_info"]["email"] == "person@empty.test"
    assert result["contact_info"]["phone"] == "+923001234567"


def test_scrape_website_rejects_corrupted_extracted_content(monkeypatch: pytest.MonkeyPatch) -> None:
    corrupted_html = "<html><body>" + ("clean business text " * 20) + ("\ufffd" * 100) + "</body></html>"
    monkeypatch.setattr(
        legacy_leads,
        "fetch_page",
        lambda url, context=None: {
            "content": corrupted_html,
            "status": "SUCCESS",
            "failure_reason": "",
            "method_used": "httpx_http2",
        },
    )
    context = legacy_leads.CrawlContext()

    website_text, method, failure_reason = legacy_leads.scrape_website(
        "https://corrupted.example",
        context=context,
    )

    assert website_text == ""
    assert method == "httpx_bs4"
    assert failure_reason == "FAILED_CORRUPTED_CONTENT"
    assert context.last_status == "corrupted_content"
    assert context.last_reason == "FAILED_CORRUPTED_CONTENT"


@pytest.mark.parametrize("method", ["readability_fallback", "meta_title_jsonld_fallback"])
def test_scrape_website_rejects_corruption_from_fallback_extractors(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    corrupted_text = ("clean " * 20) + ("\ufffd" * 50)
    monkeypatch.setattr(
        legacy_leads,
        "fetch_page",
        lambda url, context=None: {
            "content": "<html><head><title>Example</title></head><body></body></html>",
            "status": "SUCCESS",
            "failure_reason": "",
            "method_used": "httpx_http2",
        },
    )
    monkeypatch.setattr(legacy_leads, "clean_visible_text", lambda html: "")
    monkeypatch.setattr(
        legacy_leads,
        "extract_readable_text",
        lambda html: corrupted_text if method == "readability_fallback" else "",
    )
    monkeypatch.setattr(legacy_leads, "extract_title_meta_jsonld", lambda html: corrupted_text)

    website_text, actual_method, failure_reason = legacy_leads.scrape_website("https://corrupted.example")

    assert website_text == ""
    assert actual_method == method
    assert failure_reason == "FAILED_CORRUPTED_CONTENT"


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


def test_fallback_scoring_applies_name_email_and_phone_safety() -> None:
    phone_only = ScoringAgent().run(
        {
            "website": "https://prismatic-technologies.com",
            "website_text": "Our services include managed technology and customer support. " * 20,
            "company_name": 'We "want you to read what our happy customers say about working with us today"',
            "email": "info@prismatic-technologies.com",
            "phone": "+923001234567",
            "query": "technology companies in Pakistan",
            "ai_mode": "fallback",
        }
    )["lead"]
    assert phone_only["company_name"] == "Prismatic Technologies"
    assert phone_only["phone_only_eligible"] is True
    assert phone_only["email_channel_eligible"] is False
    assert phone_only["readiness"] == "phone_ready"
    assert "company name extraction fallback used" in phone_only["analysis_reason"]
    assert "generic email — phone channel only" in phone_only["analysis_reason"]

    invalid_phone = ScoringAgent().run(
        {
            "website": "https://direct-example.com",
            "website_text": "Business services and solutions. " * 20,
            "company_name": "Direct Example",
            "email": "jane@direct-example.com",
            "phone": "20122023",
            "query": "technology companies",
            "ai_mode": "fallback",
        }
    )["lead"]
    assert invalid_phone["phone"] == ""
    assert "invalid phone format removed" in invalid_phone["analysis_reason"]


def test_scoring_agent_uses_tenant_configured_groq_for_five_samples(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.models import Tenant, TenantContext
    from app.db.session import build_memory_session
    from app.services.ai_provider_service import AIProviderService

    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-groq-scoring")
    db.tenants.save(
        Tenant(
            tenant_id=tenant.tenant_id,
            name="Groq Tenant",
            settings={
                "providers": {
                    "ai": {
                        "provider": "groq",
                        "model": "llama-3.1-8b-instant",
                        "api_key_encrypted": "configured-secret-placeholder",
                        "enabled": True,
                    }
                }
            },
        )
    )

    @asynccontextmanager
    async def fake_db_session():
        yield db

    prompts: list[str] = []

    async def fake_generate(self, tenant, system_prompt, user_prompt, **kwargs):
        prompts.append(system_prompt)
        return {
            "company_summary": "Operating technology company",
            "industry": "Technology",
            "needs_it_services": True,
            "extracted_email": "",
            "lead_score": 80,
            "reason": "AI-qualified operating company.",
            "intent_analysis": {
                "buying_intent_score": 70,
                "service_demand_score": 75,
                "urgency_score": 40,
                "intent_summary": "Technology demand detected.",
                "signals": ["managed services"],
            },
        }

    monkeypatch.setattr("app.db.session.get_async_db_session", fake_db_session)
    monkeypatch.setattr(AIProviderService, "generate_text", fake_generate)

    results = [
        ScoringAgent().run(
            {
                "website": f"https://sample-{index}.example",
                "website_text": "Managed services, cloud infrastructure and business automation. " * 10,
                "company_name": f"Sample {index}",
                "email": f"person{index}@sample-{index}.example",
                "phone": "+923001234567",
                "query": "technology companies in Pakistan",
                "_ai_runtime": {"tenant": tenant},
            }
        )["lead"]
        for index in range(5)
    ]

    assert [lead["ai_mode"] for lead in results] == ["groq"] * 5
    assert len(prompts) == 5
    expected_prompt = "\n\n".join([legacy_leads.load_skill_prompt(), legacy_leads.load_claude_prompt()])
    assert prompts == [expected_prompt] * 5


def test_scoring_provider_payload_is_small_and_allowlisted() -> None:
    payload = ScoringAgent._build_provider_payload(
        {
            "website": "https://compact.example",
            "company_name": "C" * 500,
            "query": "Q" * 500,
            "website_text": "W" * 5000,
            "address": "A" * 500,
            "short_contact_summary": "S" * 1000,
            "email": "owner@compact.example",
            "phone": "+923001234567",
            "email_candidates": [
                {"email": f"person{index}@compact.example", "source": "X" * 5000}
                for index in range(6)
            ],
            "crawl_metrics": {"huge": "X" * 20_000},
            "score_breakdown": "X" * 20_000,
            "likely_emails": ["X" * 20_000],
            "raw_html": "X" * 20_000,
            "contact_text": "X" * 20_000,
        }
    )

    assert set(payload) == {
        "website", "company_name", "query", "website_text", "email_present", "phone_present", "contact_page", "short_contact_summary"
    }
    assert len(payload["company_name"]) == 120
    assert len(payload["query"]) == 160
    assert len(payload["website_text"]) == 1200
    assert len(payload["short_contact_summary"]) == 300
    assert payload["email_present"] is True
    assert payload["phone_present"] is True
    assert "address" not in payload
    assert "crawl_metrics" not in payload
    assert "email_candidates" not in payload


def test_scoring_provider_retries_413_with_500_character_text(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.models import Tenant, TenantContext
    from app.db.session import build_memory_session
    from app.services.ai_provider_service import AIProviderService

    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-groq-413")
    db.tenants.save(
        Tenant(
            tenant_id=tenant.tenant_id,
            name="Groq Retry Tenant",
            settings={
                "providers": {
                    "ai": {
                        "provider": "groq",
                        "model": "llama-3.1-8b-instant",
                        "api_key_encrypted": "configured-secret-placeholder",
                        "enabled": True,
                    }
                }
            },
        )
    )

    @asynccontextmanager
    async def fake_db_session():
        yield db

    sent_payloads: list[dict] = []

    async def fake_generate(self, tenant, system_prompt, user_prompt, **kwargs):
        payload = __import__("json").loads(user_prompt.split("Website content:\n", 1)[1].strip())
        sent_payloads.append(payload)
        if len(sent_payloads) == 1:
            request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
            response = httpx.Response(413, request=request)
            raise httpx.HTTPStatusError("Payload Too Large", request=request, response=response)
        return {
            "company_summary": "Relevant company",
            "industry": "Technology",
            "needs_it_services": True,
            "reason": "Qualified after compact retry.",
        }

    monkeypatch.setattr("app.db.session.get_async_db_session", fake_db_session)
    monkeypatch.setattr(AIProviderService, "generate_text", fake_generate)

    analysis, provider = ScoringAgent()._provider_analysis(
        ScoringAgent._build_provider_payload({"website_text": "W" * 5000}),
        {"tenant": tenant},
    )

    assert provider == "groq"
    assert analysis["needs_it_services"] is True
    assert [len(payload["website_text"]) for payload in sent_payloads] == [1200, 500]


def test_provider_relevance_and_scraped_contacts_reach_qualification(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ScoringAgent,
        "_provider_analysis",
        lambda self, text, runtime: (
            {
                "company_summary": "Relevant operating business",
                "industry": "Professional Services",
                "needs_it_services": True,
                "relevance_passed": False,
                "reason": "Provider marked this company relevant.",
            },
            "test-provider",
        ),
    )

    lead = ScoringAgent().run(
        {
            "website": "https://relevant.test",
            "website_text": "Marketing agency offering business services. " * 20,
            "website_text_length": 900,
            "company_name": "Relevant Agency",
            "email": "owner@relevant.test",
            "phone": "+923001234567",
            "scrape_status": "ok",
            "lead_status": "Pending",
            "_ai_runtime": {"tenant": object()},
        }
    )["lead"]

    assert lead["email"] == "owner@relevant.test"
    assert lead["phone"] == "+923001234567"
    assert lead["needs_it_services"] is True
    assert lead["relevance_passed"] is True
    assert lead["qualified"] is True
    assert lead["website_text_length"] == 900


def test_successful_scrape_rejection_never_reports_scrape_failed_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEADGEN_DEMO_MODE", "false")
    lead = ScoringAgent().run(
        {
            "website": "https://safe-reject.test",
            "website_text": "Business services content. " * 20,
            "company_name": "Safe Reject",
            "scrape_status": "ok",
            "lead_status": "Pending",
            "ai_mode": "fallback",
        }
    )["lead"]

    assert lead["qualified"] is False
    assert lead["skip_reason"] in ("no_contact", "relevance_not_passed")


def test_lead_analysis_prompt_excludes_spec_and_truncates_website_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(legacy_leads, "load_skill_prompt", lambda: "SCORING RUBRIC")
    monkeypatch.setattr(legacy_leads, "load_spec_prompt", lambda: "PLATFORM ARCHITECTURE MUST NOT APPEAR")
    monkeypatch.setattr(legacy_leads, "load_claude_prompt", lambda: "SCORING INSTRUCTIONS")

    system_prompt, user_prompt = legacy_leads.build_lead_analysis_prompts("x" * 10_000)

    assert system_prompt == "SCORING RUBRIC\n\nSCORING INSTRUCTIONS"
    assert "PLATFORM ARCHITECTURE" not in system_prompt
    assert "x" * legacy_leads.LEAD_ANALYSIS_WEBSITE_TEXT_LIMIT in user_prompt
    assert "x" * (legacy_leads.LEAD_ANALYSIS_WEBSITE_TEXT_LIMIT + 1) not in user_prompt


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
    assert contact["phone"] == "+15551234567"
    assert contact["email_confidence"] == "verified_email"
    assert contact["lead_readiness_score"] == 100
    sources = {item["source"] for item in contact["email_candidates"]}
    assert {"mailto", "footer", "structured_data"}.issubset(sources)


def test_contact_extraction_reads_tel_whatsapp_and_jsonld() -> None:
    html = """
    <html><body>
      <a href="tel:0300-1234567">Call</a>
      <a href="https://wa.me/923111234567">WhatsApp</a>
      <a href="https://api.whatsapp.com/send?phone=923221234567">Chat</a>
      <script type="application/ld+json">
        {"@type":"Dentist","telephone":"042-1234567","email":"clinic@example.test"}
      </script>
    </body></html>
    """

    assert legacy_leads.extract_phone_candidates_from_page(html) == [
        "+923001234567",
        "+923111234567",
        "+923221234567",
        "+92421234567",
    ]
    assert legacy_leads.extract_structured_contact_info(html)["emails"] == ["clinic@example.test"]


def test_fetch_page_does_not_retry_404(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url):
            nonlocal calls
            calls += 1
            return httpx.Response(404, request=httpx.Request("GET", url), headers={"Content-Type": "text/html"})

    monkeypatch.setattr(legacy_leads.httpx, "Client", FakeClient)
    context = legacy_leads.CrawlContext()

    result = legacy_leads.fetch_page("https://missing.example/contact", context=context)

    assert result["status"] == "FAILED"
    assert calls == 1
    assert context.metrics["pages_attempted"] == 1
    assert context.metrics["pages_404"] == 1


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
            "readiness",
            "readiness_label",
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
        assert row["readiness"] == "research_needed"
        assert row["readiness_label"] == "Research Needed"
        assert row["lead_readiness_score"] == 100
        assert row["service_reason"] == "Strong business fit"
        assert row["save_reason"] == "Strong business fit"


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
async def test_readiness_metadata_is_saved_for_email_phone_and_research_leads() -> None:
    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-readiness-metadata")
    service = LeadService(db)

    email_ready = await service.upsert_lead(
        tenant,
        Lead(
            tenant_id=tenant.tenant_id,
            company_url="https://email-ready.test",
            verified_email="jane@email-ready.test",
        ),
    )
    phone_ready = await service.upsert_lead(
        tenant,
        Lead(
            tenant_id=tenant.tenant_id,
            company_url="https://phone-ready.test",
            phone="+966 55 123 4567",
        ),
    )
    research_needed = await service.upsert_lead(
        tenant,
        Lead(
            tenant_id=tenant.tenant_id,
            company_url="https://research-needed.test",
        ),
    )

    assert email_ready.metadata["readiness"] == "email_ready"
    assert phone_ready.metadata["readiness"] == "phone_ready"
    assert research_needed.metadata["readiness"] == "research_needed"


@pytest.mark.anyio
async def test_email_crm_outreach_selection_excludes_research_needed_no_email_leads() -> None:
    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-email-crm-readiness")
    service = LeadService(db)
    email_ready = await service.upsert_lead(
        tenant,
        Lead(
            tenant_id=tenant.tenant_id,
            company="Email Ready",
            company_url="https://email-ready.test",
            verified_email="lead@email-ready.test",
            outreach_status="pending",
        ),
    )
    await service.upsert_lead(
        tenant,
        Lead(
            tenant_id=tenant.tenant_id,
            company="Research Needed",
            company_url="https://research-needed.test",
            outreach_status="pending",
            metadata={"readiness": "research_needed"},
        ),
    )

    selected = await OutreachService(db).list_pending_outreach_leads(tenant, set())

    assert [lead.id for lead in selected] == [email_ready.id]


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
async def test_lead_generation_prefers_clean_item_reason_for_service_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-clean-item-reason")

    monkeypatch.setattr(legacy_leads, "load_environment", lambda: None)
    monkeypatch.setattr(
        legacy_leads,
        "process_query",
        lambda query, seen_websites, limit, ai_mode="", niche="", location="": [
            {
                "qualified": True,
                "decision": "accepted",
                "company_name": "Clean Reason Co",
                "website": "https://clean-item-reason.test",
                "email": "info@clean-item-reason.test",
                "industry": "Technology",
                "lead_score": 82,
                "reason": "Clean Reason Co has a clear services page and verified contact details for outreach.",
                "quality_reason": "Moderate buying intent detected with available contact - worth reaching out.",
                "analysis_reason": "Clean Reason Co matches the search query and may need workflow automation.",
                "intent_summary": "target from search query",
                "email_status": "pending",
            }
        ],
    )

    await LeadGenerationAgent().run(
        AgentRequest(tenant=tenant, payload={"query": "technology companies", "limit": 1}),
        db,
    )

    saved = db.for_tenant(tenant).list("leads")[0]
    assert saved.service_reason == "Clean Reason Co has a clear services page and verified contact details for outreach."
    assert "search query" not in saved.service_reason.lower()
    assert "workflow automation" not in saved.service_reason.lower()


@pytest.mark.anyio
async def test_lead_generation_uses_quality_reason_when_item_reason_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-quality-reason")

    monkeypatch.setattr(legacy_leads, "load_environment", lambda: None)
    monkeypatch.setattr(
        legacy_leads,
        "process_query",
        lambda query, seen_websites, limit, ai_mode="", niche="", location="": [
            {
                "qualified": True,
                "decision": "accepted",
                "company_name": "Quality Reason Co",
                "website": "https://quality-reason.test",
                "email": "info@quality-reason.test",
                "industry": "Technology",
                "lead_score": 78,
                "quality_filter": {"reason": "High-intent company with verified contact - strong outreach candidate."},
                "analysis_reason": "Quality Reason Co matches the search query.",
                "email_status": "pending",
            }
        ],
    )

    await LeadGenerationAgent().run(
        AgentRequest(tenant=tenant, payload={"query": "technology companies", "limit": 1}),
        db,
    )

    saved = db.for_tenant(tenant).list("leads")[0]
    assert saved.service_reason == "High-intent company with verified contact - strong outreach candidate."


@pytest.mark.anyio
async def test_banned_analysis_and_intent_reasons_use_safe_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-banned-reasons")

    monkeypatch.setattr(legacy_leads, "load_environment", lambda: None)
    monkeypatch.setattr(
        legacy_leads,
        "process_query",
        lambda query, seen_websites, limit, ai_mode="", niche="", location="": [
            {
                "qualified": True,
                "decision": "accepted",
                "company_name": "Banned Analysis Co",
                "website": "https://banned-analysis.test",
                "email": "info@banned-analysis.test",
                "industry": "Technology",
                "lead_score": 77,
                "analysis_reason": "Banned Analysis Co matches the search query and may need workflow automation.",
                "intent_summary": "target from search query",
                "email_status": "pending",
            },
            {
                "qualified": True,
                "decision": "accepted",
                "company_name": "Banned Intent Co",
                "website": "https://banned-intent.test",
                "email": "info@banned-intent.test",
                "industry": "Software",
                "lead_score": 76,
                "analysis_reason": "",
                "intent_summary": "workflow automation may help this target from the search query",
                "email_status": "pending",
            },
            {
                "qualified": True,
                "decision": "accepted",
                "company_name": "Email Status Co",
                "website": "https://email-status-reason.test",
                "email": "info@email-status-reason.test",
                "industry": "",
                "lead_score": 74,
                "analysis_reason": "",
                "intent_summary": "",
                "email_status": "target from search query",
            },
        ],
    )

    await LeadGenerationAgent().run(
        AgentRequest(tenant=tenant, payload={"query": "software companies", "limit": 3}),
        db,
    )

    saved = db.for_tenant(tenant).list("leads")
    assert len(saved) == 3
    assert {lead.outreach_status for lead in saved} == {"pending"}
    for lead in saved:
        assert lead.service_reason
        assert "appears relevant for outreach based on its" in lead.service_reason
        lowered = lead.service_reason.lower()
        assert "search query" not in lowered
        assert "target from search query" not in lowered
        assert "workflow automation" not in lowered
        assert "email_status" not in lowered


@pytest.mark.anyio
async def test_lead_generation_defaults_new_outreach_status_to_pending_and_does_not_send(monkeypatch: pytest.MonkeyPatch) -> None:
    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-generation-pending-status")

    monkeypatch.setattr(legacy_leads, "load_environment", lambda: None)
    monkeypatch.setattr(
        legacy_leads,
        "process_query",
        lambda query, seen_websites, limit, ai_mode="", niche="", location="": [
            {
                "qualified": True,
                "decision": "accepted",
                "company_name": "Failed Raw Co",
                "website": "https://failed-raw.test",
                "email": "hello@failed-raw.test",
                "phone": "",
                "address": "",
                "industry": "Technology",
                "lead_score": 70,
                "analysis_reason": "The company appears relevant for outreach.",
                "email_status": "failed",
            },
            {
                "qualified": True,
                "decision": "accepted",
                "company_name": "No Email Raw Co",
                "website": "https://no-email-raw.test",
                "email": "",
                "phone": "+923001234567",
                "address": "",
                "industry": "Technology",
                "lead_score": 45,
                "analysis_reason": "The company can be contacted manually.",
                "email_status": "no_email",
            },
        ],
    )

    result = await LeadGenerationAgent().run(
        AgentRequest(tenant=tenant, payload={"query": "technology companies", "limit": 2}),
        db,
    )

    saved = db.for_tenant(tenant).list("leads")
    emails = db.for_tenant(tenant).list("emails")
    assert result["status"] == "SUCCESS"
    assert len(saved) == 2
    assert emails == []
    assert {lead.outreach_status for lead in saved} == {"pending"}
    assert {lead.status for lead in saved} == {"pending"}
    assert {lead.metadata.get("raw_email_status") for lead in saved} == {"failed", "no_email"}


@pytest.mark.anyio
async def test_lead_generation_links_saved_leads_to_background_job(monkeypatch: pytest.MonkeyPatch) -> None:
    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-generation-job-link")

    monkeypatch.setattr(legacy_leads, "load_environment", lambda: None)
    monkeypatch.setattr(
        legacy_leads,
        "process_query",
        lambda query, seen_websites, limit, ai_mode="", niche="", location="": [
            {
                "qualified": True,
                "decision": "accepted",
                "company_name": "Linked Job Co",
                "website": "https://linked-job.test",
                "email": "hello@linked-job.test",
                "phone": "",
                "address": "",
                "industry": "Technology",
                "lead_score": 72,
                "analysis_reason": "The company appears relevant for outreach.",
                "email_status": "pending",
            }
        ],
    )

    result = await LeadGenerationAgent().run(
        AgentRequest(tenant=tenant, payload={"query": "technology companies", "limit": 1, "_job_id": "job-linked-123"}),
        db,
    )

    saved = db.for_tenant(tenant).list("leads")
    assert result["status"] == "SUCCESS"
    assert len(saved) == 1
    assert saved[0].job_id == "job-linked-123"


@pytest.mark.anyio
async def test_free_plan_fallback_does_not_auto_qualify_lead_without_provider_review(monkeypatch: pytest.MonkeyPatch) -> None:
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

    saved = db.for_tenant(tenant).list("leads")
    assert result["status"] == "SUCCESS"
    assert result["data"]["saved_leads"] == 1
    assert len(saved) == 1
    assert saved[0].verified_email == "info@logistics.example"


@pytest.mark.anyio
async def test_free_plan_fallback_rejects_lead_without_contact(monkeypatch: pytest.MonkeyPatch) -> None:
    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-no-contact")

    monkeypatch.setattr(legacy_leads, "load_environment", lambda: None)
    monkeypatch.setattr(legacy_leads, "search_google", lambda query: [{"link": "https://nocontact.example"}])
    monkeypatch.setattr(legacy_leads, "extract_websites", lambda results: ["https://nocontact.example"])
    monkeypatch.setattr(
        legacy_leads,
        "extract_contact_info",
        lambda website, context=None: {
            "company_name": "No Contact Co",
            "email": "",
            "phone": "",
            "address": "",
            "contact_page": "",
        },
    )
    monkeypatch.setattr(
        legacy_leads,
        "scrape_website",
        lambda website, context=None: ("Some generic website text with no business signals.", "requests_bs4", ""),
    )
    monkeypatch.setattr(
        legacy_leads,
        "analyze_lead_with_claude",
        lambda website_text: pytest.fail("Claude should not be called for Free fallback lead generation."),
    )

    result = await LeadGenerationAgent().run(
        AgentRequest(tenant=tenant, payload={"query": "test companies", "limit": 1}),
        db,
    )

    saved = db.for_tenant(tenant).list("leads")
    assert result["status"] == "SUCCESS"
    assert result["data"]["saved_leads"] == 0
    assert saved == []


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
    assert saved.service_reason == "No Claude Co appears relevant for outreach based on its business profile and contact availability."
    assert saved.industry == "Other"
    assert saved.country == "Saudi Arabia"
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
            verified_email="jane@acme.test",
            email="jane@acme.test",
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
    assert fake_provider.sent[0][1] == "jane@acme.test"
    assert fake_provider.sent[0][2]
    assert "unsubscribe" in fake_provider.sent[0][3].lower()
