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
    assert saved.metadata["outreach_error"] == "gmail_send_failed"
    assert saved.metadata["outreach_error_at"]
    assert len(emails) == 1
    assert emails[0].status == "failed"
    assert emails[0].metadata["error"] == "gmail_send_failed"


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
    assert saved.metadata["outreach_error"] == "missing_gmail_credentials"
    assert saved.metadata["outreach_error_at"]
    assert len(emails) == 1
    assert emails[0].metadata["error"] == "missing_gmail_credentials"


@pytest.mark.anyio
async def test_invalid_or_missing_email_persists_no_verified_email_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-no-email")
    db.tenants.save(_tenant_record(tenant.tenant_id))
    await _configure_gmail(db, tenant, monkeypatch)
    lead = db.for_tenant(tenant).save(
        "leads",
        Lead(
            tenant_id=tenant.tenant_id,
            company="No Email Co",
            company_url="https://no-email.test",
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
    assert result["failed_messages"] == 1
    assert fake_provider.sent == []
    assert saved.status == "failed"
    assert saved.outreach_status == "failed"
    assert saved.metadata["outreach_error"] == "no_verified_email"
    assert saved.metadata["outreach_error_at"]
    assert len(emails) == 1
    assert emails[0].metadata["error"] == "no_verified_email"


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
            Lead(tenant_id=tenant.tenant_id, company="Failed", status="failed", outreach_status="failed", metadata={}),
        )
        response = await client.get("/outreach/preflight", headers=_auth_headers(token))

    assert response.status_code == 200
    payload = response.json()
    assert payload["gmail_connected"] is False
    assert payload["sendable_count"] == 1
    assert payload["no_email_count"] == 1
    assert payload["failed_without_reason_count"] == 1
    assert payload["sample_errors"][0]["outreach_error"] == "unknown_outreach_failure"


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
    assert "error_type=gmail_send_failed" in caplog.text
    assert "status=failed" in caplog.text
    assert "lead@acme-logistics.test" not in caplog.text
    assert "Would it be worth a quick" not in caplog.text
    assert "gmail down" not in caplog.text
