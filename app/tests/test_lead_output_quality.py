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
from app.core.models import Lead, Tenant, TenantContext
from app.db.session import build_memory_session
from app.providers.base import ProviderSendResult
from app.services.lead_service import LeadService
from app.services.provider_credential_service import ProviderCredentialService


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


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
            "service_reason",
            "industry",
            "score",
            "outreach_status",
            "outreach_error",
            "followup_count",
            "reply_status",
            "last_reply_at",
        ]
        assert row["company_url"] == "https://acme.test"
        assert row["country"] == ""
        assert row["verified_email"] == "info@acme.test"
        assert row["phone"] == ""
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

    assert saved.service_reason == ""


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
    assert saved.service_reason == ""


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
async def test_same_domain_generic_email_is_kept_with_low_confidence() -> None:
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
    assert saved.metadata["email_confidence"] == "low"


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
    assert saved.metadata["email_confidence"] == "high"
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
async def test_missing_claude_industry_remains_other_and_reason_empty(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
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
    assert saved.service_reason == ""
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
