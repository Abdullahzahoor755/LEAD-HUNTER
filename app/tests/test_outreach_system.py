from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

import app.agents.outreach as outreach_module
from app.agents.base import AgentRequest
from app.agents.outreach import OutreachAgent
from app.core.models import Email, Lead, Tenant, TenantContext
from app.db.session import build_memory_session
from app.providers.base import ProviderSendResult
from app.services.ai_provider_service import AIProviderService
from app.services.lead_service import LeadService
from app.services.outreach_email_service import OutreachEmailService
from app.services.outreach_service import OutreachService
from app.services.provider_credential_service import ProviderCredentialService


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
    assert len(emails) == 1
    assert emails[0].status == "failed"


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
