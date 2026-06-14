from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import app.agents.outreach as outreach_module
from app.agents.base import AgentRequest
from app.agents.outreach import OutreachAgent
from app.api.app import create_fastapi_app
from app.core.models import Email, Lead, Tenant, TenantContext
from app.db.session import build_memory_session
from app.providers.base import ProviderSendResult
from app.services.ai_provider_service import AIProviderService
from app.services.lead_service import LeadService
from app.services.outreach_email_service import OutreachEmailService
from app.services.outreach_service import OutreachService
from app.services.provider_credential_service import ProviderCredentialService
from scripts.backfill_outreach_errors import backfill_outreach_errors


class _FakeGmailProvider:
    def __init__(self) -> None:
        self.sent = []

    async def send(self, account, request):
        self.sent.append((account.tenant_id, request.to, request.subject, request.body, request.thread_id))
        return ProviderSendResult(message_id="msg-1", thread_id=request.thread_id or "thread-1", raw={"id": "msg-1", "threadId": "thread-1"})

    async def fetch_replies(self, account, cursor: str = ""):
        return []


class _FailingGmailProvider:
    async def send(self, account, request):
        raise RuntimeError("gmail down")


def _tenant_record(tenant_id: str = "tenant-outreach", settings: dict | None = None) -> Tenant:
    return Tenant(
        tenant_id=tenant_id,
        name="Tenant",
        slug=tenant_id,
        subscription_plan="Pro",
        settings=dict(settings or {}),
    )


async def _configure_gmail(db, tenant: TenantContext, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.configs.settings.settings.secret_encryption_key", "test-key")
    await ProviderCredentialService(db).save_gmail_credentials(
        tenant,
        {
            "client_id": "client",
            "client_secret": "secret",
            "refresh_token": "refresh-token",
            "access_token": "access-token",
            "email_address": "sender@tenant.test",
            "scopes": ["gmail.send", "gmail.readonly"],
        },
    )


async def _save_pending_lead(db, tenant: TenantContext) -> Lead:
    return await LeadService(db).upsert_lead(
        tenant,
        Lead(
            tenant_id=tenant.tenant_id,
            company="Acme Logistics",
            company_url="https://acme-logistics.test",
            verified_email="lead@acme-logistics.test",
            email="lead@acme-logistics.test",
            industry="logistics",
            country="UAE",
            service_reason="their site has a clear services page but no obvious lead capture flow",
            status="pending",
            outreach_status="pending",
            metadata={
                "agency_kit": {"recommended_service": "lead capture automation"},
                "offer_match": {"recommended_offer": "follow-up workflow"},
            },
        ),
    )


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.anyio
async def test_outreach_email_generation_works_without_anthropic_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-email-service")
    db.tenants.save(_tenant_record(tenant.tenant_id))
    lead = await _save_pending_lead(db, tenant)

    result = await OutreachEmailService(db).generate_outreach_email(tenant, lead)

    assert result["mode"] == "fallback"
    assert result["subject"]
    assert "quick" in result["body"].lower() or "chat" in result["body"].lower()
    assert "Acme Logistics" in result["body"]
    assert result["human_score"] >= 85


@pytest.mark.anyio
async def test_outreach_email_generation_is_human_style(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-human-email")
    db.tenants.save(_tenant_record(tenant.tenant_id))
    lead = await _save_pending_lead(db, tenant)

    result = await OutreachEmailService(db).generate_outreach_email(tenant, lead)
    body = result["body"]
    lowered = body.lower()
    first_sentence = body.strip().split(".", 1)[0].lower()
    words = body.replace("\n", " ").split()

    assert first_sentence.startswith(("i noticed", "i saw", "i came across", "i was looking at"))
    assert len(words) <= 90
    assert result["human_score"] >= 85
    assert result["company_summary"]
    assert result["likely_service_category"]
    assert result["personalized_observation"]
    for banned in [
        "search query",
        "lead source",
        "scraped data",
        "target matching",
        "target from search query",
        "target from the search query",
        "matches the search query",
        "workflow automation may help",
        "may need workflow automation",
        "ai generated",
        "we provide ai services",
        "i'm our team",
    ]:
        assert banned not in lowered


def test_human_score_rejects_banned_ai_phrases() -> None:
    service = OutreachEmailService(build_memory_session())
    body = (
        "I noticed Acme matches the search query.\n\n"
        "Workflow automation may help.\n\n"
        "Open to a quick 10-minute call?"
    )

    assert service.human_score("Quick idea", body) < 85


@pytest.mark.anyio
async def test_followup_sequence_sounds_distinct(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-human-followups")
    db.tenants.save(_tenant_record(tenant.tenant_id))
    lead = await _save_pending_lead(db, tenant)
    service = OutreachEmailService(db)

    first = await service.generate_followup_email(tenant, lead, 1, "Initial", "Body")
    second = await service.generate_followup_email(tenant, lead, 2, "Initial", "Body")
    third = await service.generate_followup_email(tenant, lead, 3, "Initial", "Body")

    assert "top of your inbox" in first["body"]
    assert "short example" in second["body"]
    assert "close the loop" in third["body"].lower()
    assert len({first["body"], second["body"], third["body"]}) == 3


@pytest.mark.anyio
async def test_active_outreach_path_does_not_call_legacy_generate_cold_email(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-active-outreach")
    db.tenants.save(_tenant_record(tenant.tenant_id))
    await _configure_gmail(db, tenant, monkeypatch)
    lead = await _save_pending_lead(db, tenant)
    fake_provider = _FakeGmailProvider()
    monkeypatch.setattr(outreach_module, "build_provider_registry", lambda: {"gmail": fake_provider})

    import leads

    monkeypatch.setattr(leads, "generate_cold_email", lambda *_args, **_kwargs: pytest.fail("legacy generate_cold_email should not be called"))

    result = await OutreachAgent().run(AgentRequest(tenant=tenant, payload={}), db)

    saved = db.for_tenant(tenant).get("leads", lead.id)
    emails = db.for_tenant(tenant).list("emails")
    assert result["sent_messages"] == 1
    assert result["failed_messages"] == 0
    assert len(fake_provider.sent) == 1
    assert saved.status == "sent"
    assert saved.outreach_status == "sent"
    assert len(emails) == 1
    assert emails[0].status == "sent"


@pytest.mark.anyio
async def test_successful_send_persists_status_sent_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-status-sent")
    db.tenants.save(_tenant_record(tenant.tenant_id))
    lead = await _save_pending_lead(db, tenant)
    now = datetime.now(timezone.utc)

    await OutreachService(db).mark_outreach_result(
        tenant=tenant,
        lead=lead,
        subject="Hello",
        body="Body",
        message_id="msg-1",
        thread_id="thread-1",
        status="sent",
        sent_at_iso=now.isoformat(),
        sent_at_dt=now,
    )

    saved = db.for_tenant(tenant).get("leads", lead.id)
    assert saved.status == "sent"
    assert saved.outreach_status == "sent"


@pytest.mark.anyio
async def test_retry_failed_verified_email_lead_clears_old_error_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-retry-success")
    db.tenants.save(_tenant_record(tenant.tenant_id))
    await _configure_gmail(db, tenant, monkeypatch)
    lead = db.for_tenant(tenant).save(
        "leads",
        Lead(
            tenant_id=tenant.tenant_id,
            company="Retry Co",
            company_url="https://retry.test",
            email="lead@retry.test",
            verified_email="lead@retry.test",
            status="failed",
            outreach_status="failed",
            metadata={"outreach_error": "gmail_send_failed", "outreach_error_at": "2026-01-01T00:00:00+00:00"},
        ),
    )
    fake_provider = _FakeGmailProvider()
    monkeypatch.setattr(outreach_module, "build_provider_registry", lambda: {"gmail": fake_provider})

    result = await OutreachAgent().run(AgentRequest(tenant=tenant, payload={}), db)

    saved = db.for_tenant(tenant).get("leads", lead.id)
    assert result["sent_messages"] == 1
    assert result["failed_messages"] == 0
    assert fake_provider.sent[0][1] == "lead@retry.test"
    assert saved.status == "sent"
    assert saved.outreach_status == "sent"
    assert "outreach_error" not in saved.metadata
    assert "outreach_error_at" not in saved.metadata


@pytest.mark.anyio
async def test_failed_send_persists_failed_status_and_email_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-send-failed")
    db.tenants.save(_tenant_record(tenant.tenant_id))
    await _configure_gmail(db, tenant, monkeypatch)
    lead = await _save_pending_lead(db, tenant)
    monkeypatch.setattr(outreach_module, "build_provider_registry", lambda: {"gmail": _FailingGmailProvider()})

    result = await OutreachAgent().run(AgentRequest(tenant=tenant, payload={}), db)

    saved = db.for_tenant(tenant).get("leads", lead.id)
    emails = db.for_tenant(tenant).list("emails")
    assert result["sent_messages"] == 0
    assert result["failed_messages"] == 1
    assert saved.status == "failed"
    assert saved.outreach_status == "failed"
    assert saved.metadata["outreach_error"] == "gmail_unknown_send_error"
    assert saved.metadata["outreach_error_at"]
    assert len(emails) == 1
    assert emails[0].status == "failed"
    assert emails[0].metadata["error"] == "gmail_unknown_send_error"


@pytest.mark.anyio
async def test_missing_gmail_credentials_persists_failure_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-missing-gmail")
    db.tenants.save(_tenant_record(tenant.tenant_id))
    lead = await _save_pending_lead(db, tenant)

    result = await OutreachAgent().run(AgentRequest(tenant=tenant, payload={}), db)

    saved = db.for_tenant(tenant).get("leads", lead.id)
    emails = db.for_tenant(tenant).list("emails")
    assert result["sent_messages"] == 0
    assert result["failed_messages"] == 1
    assert saved.status == "failed"
    assert saved.outreach_status == "failed"
    assert saved.metadata["outreach_error"] == "gmail_not_connected"
    assert saved.metadata["outreach_error_at"]
    assert len(emails) == 1
    assert emails[0].metadata["error"] == "gmail_not_connected"


@pytest.mark.anyio
async def test_phone_only_lead_is_not_marked_failed_by_email_outreach(monkeypatch: pytest.MonkeyPatch) -> None:
    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-phone-only")
    db.tenants.save(_tenant_record(tenant.tenant_id))
    await _configure_gmail(db, tenant, monkeypatch)
    lead = db.for_tenant(tenant).save(
        "leads",
        Lead(
            tenant_id=tenant.tenant_id,
            company="Phone Only Co",
            company_url="https://phone-only.test",
            phone="+923000000000",
            status="pending",
            outreach_status="pending",
        ),
    )
    fake_provider = _FakeGmailProvider()
    monkeypatch.setattr(outreach_module, "build_provider_registry", lambda: {"gmail": fake_provider})

    result = await OutreachAgent().run(AgentRequest(tenant=tenant, payload={}), db)

    saved = db.for_tenant(tenant).get("leads", lead.id)
    emails = db.for_tenant(tenant).list("emails")
    assert result["sent_messages"] == 0
    assert result["failed_messages"] == 0
    assert fake_provider.sent == []
    assert saved.status == "pending"
    assert saved.outreach_status == "pending"
    assert "outreach_error" not in saved.metadata
    assert emails == []


@pytest.mark.anyio
async def test_leads_returns_unknown_for_failed_blank_outreach_error() -> None:
    db = build_memory_session()
    app = create_fastapi_app(db=db)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        signup = await client.post(
            "/signup",
            json={
                "tenant_id": "tenant-failed-blank",
                "tenant_name": "Tenant Failed Blank",
                "tenant_slug": "tenant-failed-blank",
                "email": "owner@failed-blank.test",
                "password": "secret123",
                "full_name": "Owner",
            },
        )
        token = signup.json()["token"]
        tenant = TenantContext(tenant_id="tenant-failed-blank")
        db.for_tenant(tenant).save(
            "leads",
            Lead(
                tenant_id=tenant.tenant_id,
                company="Old Failed Co",
                company_url="https://old-failed.test",
                verified_email="lead@old-failed.test",
                email="lead@old-failed.test",
                status="failed",
                outreach_status="failed",
                metadata={},
            ),
        )
        response = await client.get("/leads", headers=_auth_headers(token))

    assert response.status_code == 200
    assert response.json()["items"][0]["outreach_error"] == "unknown_outreach_failure"


@pytest.mark.anyio
async def test_outreach_preflight_counts_sendable_no_email_and_unknown_failures() -> None:
    db = build_memory_session()
    app = create_fastapi_app(db=db)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        signup = await client.post(
            "/signup",
            json={
                "tenant_id": "tenant-preflight",
                "tenant_name": "Tenant Preflight",
                "tenant_slug": "tenant-preflight",
                "email": "owner@preflight.test",
                "password": "secret123",
                "full_name": "Owner",
                "plan": "Pro",
            },
        )
        token = signup.json()["token"]
        tenant = TenantContext(tenant_id="tenant-preflight")
        db.for_tenant(tenant).save(
            "leads",
            Lead(tenant_id=tenant.tenant_id, company="Good", email="lead@good.test", verified_email="lead@good.test", status="pending", outreach_status="pending"),
        )
        db.for_tenant(tenant).save(
            "leads",
            Lead(tenant_id=tenant.tenant_id, company="No Email", status="pending", outreach_status="pending"),
        )
        db.for_tenant(tenant).save(
            "leads",
            Lead(
                tenant_id=tenant.tenant_id,
                company="Failed",
                email="lead@failed.test",
                verified_email="lead@failed.test",
                status="failed",
                outreach_status="failed",
                metadata={},
            ),
        )
        response = await client.get("/outreach/preflight", headers=_auth_headers(token))

    assert response.status_code == 200
    payload = response.json()
    assert payload["gmail_connected"] is False
    assert payload["pending_sendable_count"] == 1
    assert payload["retryable_failed_count"] == 1
    assert payload["sendable_count"] == 2
    assert payload["already_sent_count"] == 0
    assert payload["no_email_count"] == 1
    assert payload["failed_without_reason_count"] == 1
    assert payload["sample_errors"][0]["outreach_error"] == "unknown_outreach_failure"


@pytest.mark.anyio
async def test_outreach_preflight_with_zero_verified_email_leads() -> None:
    db = build_memory_session()
    app = create_fastapi_app(db=db)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        signup = await client.post(
            "/signup",
            json={
                "tenant_id": "tenant-preflight-zero-email",
                "tenant_name": "Tenant Preflight Zero Email",
                "tenant_slug": "tenant-preflight-zero-email",
                "email": "owner@preflight-zero.test",
                "password": "secret123",
                "full_name": "Owner",
                "plan": "Pro",
            },
        )
        token = signup.json()["token"]
        tenant = TenantContext(tenant_id="tenant-preflight-zero-email")
        db.for_tenant(tenant).save(
            "leads",
            Lead(
                tenant_id=tenant.tenant_id,
                company="Phone Only",
                company_url="https://phone-only.test",
                phone="+923000000000",
                status="pending",
                outreach_status="pending",
            ),
        )
        response = await client.get("/outreach/preflight", headers=_auth_headers(token))

    assert response.status_code == 200
    payload = response.json()
    assert payload["pending_sendable_count"] == 0
    assert payload["retryable_failed_count"] == 0
    assert payload["sendable_count"] == 0
    assert payload["no_email_count"] == 1
    assert payload["gmail_connected"] is False


@pytest.mark.anyio
async def test_outreach_preflight_with_one_verified_email_lead() -> None:
    db = build_memory_session()
    app = create_fastapi_app(db=db)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        signup = await client.post(
            "/signup",
            json={
                "tenant_id": "tenant-preflight-one-email",
                "tenant_name": "Tenant Preflight One Email",
                "tenant_slug": "tenant-preflight-one-email",
                "email": "owner@preflight-one.test",
                "password": "secret123",
                "full_name": "Owner",
                "plan": "Pro",
            },
        )
        token = signup.json()["token"]
        tenant = TenantContext(tenant_id="tenant-preflight-one-email")
        db.for_tenant(tenant).save(
            "leads",
            Lead(
                tenant_id=tenant.tenant_id,
                company="Email Ready",
                company_url="https://email-ready.test",
                verified_email="lead@email-ready.test",
                email="lead@email-ready.test",
                status="pending",
                outreach_status="pending",
            ),
        )
        response = await client.get("/outreach/preflight", headers=_auth_headers(token))

    assert response.status_code == 200
    payload = response.json()
    assert payload["pending_sendable_count"] == 1
    assert payload["retryable_failed_count"] == 0
    assert payload["sendable_count"] == 1
    assert payload["no_email_count"] == 0


@pytest.mark.anyio
async def test_outreach_preflight_counts_retryable_failed_and_matches_selected_leads() -> None:
    db = build_memory_session()
    app = create_fastapi_app(db=db)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        signup = await client.post(
            "/signup",
            json={
                "tenant_id": "tenant-preflight-retryable",
                "tenant_name": "Tenant Preflight Retryable",
                "tenant_slug": "tenant-preflight-retryable",
                "email": "owner@preflight-retryable.test",
                "password": "secret123",
                "full_name": "Owner",
                "plan": "Pro",
            },
        )
        token = signup.json()["token"]
        tenant = TenantContext(tenant_id="tenant-preflight-retryable")
        pending = db.for_tenant(tenant).save(
            "leads",
            Lead(
                tenant_id=tenant.tenant_id,
                company="Pending",
                email="lead@pending.test",
                verified_email="lead@pending.test",
                status="pending",
                outreach_status="pending",
            ),
        )
        failed = db.for_tenant(tenant).save(
            "leads",
            Lead(
                tenant_id=tenant.tenant_id,
                company="Failed",
                email="lead@failed-retry.test",
                verified_email="lead@failed-retry.test",
                status="failed",
                outreach_status="failed",
                metadata={"outreach_error": "gmail_send_failed"},
            ),
        )
        db.for_tenant(tenant).save(
            "leads",
            Lead(
                tenant_id=tenant.tenant_id,
                company="Sent",
                email="lead@sent.test",
                verified_email="lead@sent.test",
                status="sent",
                outreach_status="sent",
            ),
        )
        db.for_tenant(tenant).save(
            "leads",
            Lead(tenant_id=tenant.tenant_id, company="No Email", status="pending", outreach_status="pending"),
        )
        db.for_tenant(tenant).save(
            "leads",
            Lead(
                tenant_id=tenant.tenant_id,
                company="Sending",
                email="lead@sending.test",
                verified_email="lead@sending.test",
                status="sending",
                outreach_status="sending",
            ),
        )
        response = await client.get("/outreach/preflight", headers=_auth_headers(token))

    selected = await OutreachService(db).list_pending_outreach_leads(tenant, set())
    selected_ids = {lead.id for lead in selected}
    payload = response.json()

    assert response.status_code == 200
    assert payload["pending_sendable_count"] == 1
    assert payload["retryable_failed_count"] == 1
    assert payload["sendable_count"] == 2
    assert payload["already_sent_count"] == 1
    assert payload["no_email_count"] == 1
    assert selected_ids == {pending.id, failed.id}


@pytest.mark.anyio
async def test_backfill_outreach_errors_dry_run_and_apply() -> None:
    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-backfill")
    db.tenants.save(_tenant_record(tenant.tenant_id))
    lead = db.for_tenant(tenant).save(
        "leads",
        Lead(
            tenant_id=tenant.tenant_id,
            company="Backfill Co",
            company_url="https://backfill.test",
            status="failed",
            outreach_status="failed",
            metadata={"preserve": "yes"},
        ),
    )

    dry_run = await backfill_outreach_errors(db, tenant.tenant_id)
    dry_saved = db.for_tenant(tenant).get("leads", lead.id)
    applied = await backfill_outreach_errors(db, tenant.tenant_id, apply=True)
    saved = db.for_tenant(tenant).get("leads", lead.id)

    assert dry_run["dry_run"] is True
    assert dry_run["matched_count"] == 1
    assert dry_run["updated_count"] == 0
    assert "outreach_error" not in dry_saved.metadata
    assert applied["dry_run"] is False
    assert applied["updated_count"] == 1
    assert saved.metadata["preserve"] == "yes"
    assert saved.metadata["outreach_error"] == "unknown_outreach_failure"
    assert saved.metadata["outreach_error_at"]


@pytest.mark.anyio
async def test_reply_monitor_sees_sent_leads_after_real_outreach_result(monkeypatch: pytest.MonkeyPatch) -> None:
    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-reply-candidates")
    db.tenants.save(_tenant_record(tenant.tenant_id))
    await _configure_gmail(db, tenant, monkeypatch)
    await _save_pending_lead(db, tenant)
    monkeypatch.setattr(outreach_module, "build_provider_registry", lambda: {"gmail": _FakeGmailProvider()})

    await OutreachAgent().run(AgentRequest(tenant=tenant, payload={}), db)

    candidates = await OutreachService(db).list_reply_candidates(tenant)
    assert len(candidates) == 1
    assert candidates[0]["thread_id"] == "thread-1"


@pytest.mark.anyio
async def test_followup_generator_works_without_anthropic_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-followup-generator")
    db.tenants.save(_tenant_record(tenant.tenant_id))
    lead = await _save_pending_lead(db, tenant)

    result = await OutreachEmailService(db).generate_followup_email(
        tenant,
        lead,
        followup_number=1,
        previous_subject="Initial note",
        previous_body="Initial body",
    )

    assert result["mode"] == "fallback"
    assert result["subject"]
    assert "follow" in result["body"].lower() or "chat" in result["body"].lower()


@pytest.mark.anyio
async def test_ai_provider_failure_falls_back_safely(monkeypatch: pytest.MonkeyPatch) -> None:
    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-ai-fail")
    db.tenants.save(
        _tenant_record(
            tenant.tenant_id,
            settings={
                "providers": {
                    "ai": {
                        "provider": "openai",
                        "model": "gpt-4o-mini",
                        "api_key_encrypted": "encrypted-key",
                        "enabled": True,
                    }
                }
            },
        )
    )
    lead = await _save_pending_lead(db, tenant)

    async def fail_generate(self, *args, **kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(AIProviderService, "generate_text", fail_generate)

    result = await OutreachEmailService(db).generate_outreach_email(tenant, lead)

    assert result["mode"] == "fallback"
    assert result["ai_error"] == "AI provider failed; fallback used"
    assert result["subject"]
    assert result["body"]


@pytest.mark.anyio
async def test_production_audit_logs_do_not_include_full_body_or_recipient(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DEBUG", "false")
    caplog.set_level(logging.INFO)
    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-prod-logs")
    db.tenants.save(_tenant_record(tenant.tenant_id))
    await _configure_gmail(db, tenant, monkeypatch)
    await _save_pending_lead(db, tenant)
    monkeypatch.setattr(outreach_module, "build_provider_registry", lambda: {"gmail": _FakeGmailProvider()})

    await OutreachAgent().run(AgentRequest(tenant=tenant, payload={}), db)

    assert "OUTREACH_AUDIT" not in caplog.text
    assert "lead@acme-logistics.test" not in caplog.text
    assert "Would it be worth a quick" not in caplog.text


@pytest.mark.anyio
async def test_production_failure_log_uses_safe_fields(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DEBUG", "false")
    caplog.set_level(logging.WARNING)
    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-prod-failure-log")
    db.tenants.save(_tenant_record(tenant.tenant_id))
    await _configure_gmail(db, tenant, monkeypatch)
    await _save_pending_lead(db, tenant)
    monkeypatch.setattr(outreach_module, "build_provider_registry", lambda: {"gmail": _FailingGmailProvider()})

    await OutreachAgent().run(AgentRequest(tenant=tenant, payload={}), db)

    assert "tenant-prod-failure-log" in caplog.text
    assert "error_type=gmail_unknown_send_error" in caplog.text
    assert "status=failed" in caplog.text
    assert "lead@acme-logistics.test" not in caplog.text
    assert "Would it be worth a quick" not in caplog.text
    assert "gmail down" not in caplog.text
