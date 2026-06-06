from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import app.agents.followup as followup_module
import app.agents.reply_monitor as reply_monitor_module
from app.agents.base import AgentRequest, BaseAgent
from app.agents.followup import FollowupAgent
from app.agents.registry import AgentRegistry
from app.agents.reply_monitor import ReplyMonitorAgent
from app.core.models import Email, Lead, Tenant, TenantContext
from app.db.session import build_memory_session
from app.providers.base import ProviderReply, ProviderSendResult
from app.services.job_service import JobService
from app.services.provider_credential_service import ProviderCredentialService
from app.workers.jobs import AsyncJobQueue


class _FakeGmailProvider:
    def __init__(self) -> None:
        self.sent = []

    async def send(self, account, request):
        self.sent.append((account.tenant_id, request.to, request.subject, request.body, request.thread_id))
        return ProviderSendResult(message_id="msg-1", thread_id=request.thread_id or "thread-1", raw={"id": "msg-1"})

    async def fetch_replies(self, account, cursor: str = ""):
        return [
            ProviderReply(
                from_address="Lead <lead@example.com>",
                subject="Re: hello",
                body="Yes, let's talk.",
                message_id="reply-1",
                thread_id=cursor,
                metadata={
                    "internal_date": "1710000000000",
                    "raw": {
                        "id": "reply-1",
                        "threadId": cursor,
                        "internalDate": "1710000000000",
                    },
                },
            )
        ]


@pytest.mark.anyio
async def test_followup_worker_uses_tenant_scoped_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-followup")
    monkeypatch.setattr("app.configs.settings.settings.secret_encryption_key", "test-key")
    db.tenants.save(Tenant(tenant_id=tenant.tenant_id, name="Tenant", slug="tenant-followup", subscription_plan="Pro"))
    await ProviderCredentialService(db).save_gmail_credentials(
        tenant,
        {
            "client_id": "client",
            "client_secret": "secret",
            "refresh_token": "refresh-token",
            "access_token": "access-token",
            "email_address": "sender@tenant.test",
            "scopes": ["gmail.readonly"],
        },
    )
    lead = db.for_tenant(tenant).save(
        "leads",
        Lead(
            tenant_id=tenant.tenant_id,
            company="Acme",
            email="lead@example.com",
            status="sent",
            metadata={
                "FollowupCount": 0,
                "LastContactedAt": (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(),
                "GmailThreadId": "thread-1",
            },
        ),
    )
    db.for_tenant(tenant).save(
        "emails",
        Email(
            tenant_id=tenant.tenant_id,
            lead_id=lead.id,
            subject="Initial outreach",
            body="First note",
            provider_thread_id="thread-1",
            sent_at=datetime.now(timezone.utc) - timedelta(days=3),
            status="sent",
        ),
    )
    fake_provider = _FakeGmailProvider()
    monkeypatch.setattr(followup_module, "build_provider_registry", lambda: {"gmail": fake_provider})

    result = await FollowupAgent().run(AgentRequest(tenant=tenant, payload={}), db)

    saved_followups = db.for_tenant(tenant).list("followups")
    assert result["sent_followups"] == 1
    assert len(saved_followups) == 1
    assert fake_provider.sent[0][0] == tenant.tenant_id
    assert "unsubscribe" in fake_provider.sent[0][3].lower()


@pytest.mark.anyio
async def test_reply_monitor_persists_replies_with_tenant_oauth(monkeypatch: pytest.MonkeyPatch) -> None:
    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-replies")
    monkeypatch.setattr("app.configs.settings.settings.secret_encryption_key", "test-key")
    db.tenants.save(Tenant(tenant_id=tenant.tenant_id, name="Tenant", slug="tenant-replies", subscription_plan="Pro"))
    await ProviderCredentialService(db).save_gmail_credentials(
        tenant,
        {
            "client_id": "client",
            "client_secret": "secret",
            "refresh_token": "refresh-token",
            "access_token": "access-token",
            "email_address": "sender@tenant.test",
            "scopes": ["gmail.readonly"],
        },
    )
    lead = db.for_tenant(tenant).save(
        "leads",
        Lead(
            tenant_id=tenant.tenant_id,
            company="Beta",
            email="lead@example.com",
            status="sent",
            metadata={"GmailThreadId": "thread-1", "MeetingRequested": "No"},
        ),
    )
    db.for_tenant(tenant).save(
        "emails",
        Email(
            tenant_id=tenant.tenant_id,
            lead_id=lead.id,
            subject="Initial outreach",
            body="Hello",
            provider_thread_id="thread-1",
            sent_at=datetime.now(timezone.utc) - timedelta(days=1),
            status="sent",
        ),
    )
    fake_provider = _FakeGmailProvider()
    monkeypatch.setattr(reply_monitor_module, "build_provider_registry", lambda: {"gmail": fake_provider})

    async def _classify(self, company: str, sender_email: str, subject: str, reply_text: str):
        return {
            "classification": "Interested",
            "sentiment": "positive",
            "lead_temperature": "hot",
            "reason": "Asked to talk",
            "confidence_score": 93,
            "next_action_suggestion": "Book a meeting",
        }

    monkeypatch.setattr(reply_monitor_module.ReplyAiService, "classify", _classify)
    result = await ReplyMonitorAgent().run(AgentRequest(tenant=tenant, payload={"mode": "once"}), db)

    replies = db.for_tenant(tenant).list("replies")
    updated_lead = db.for_tenant(tenant).get("leads", lead.id)
    assert result["checked"] == 1
    assert len(replies) == 1
    assert replies[0].classification == "Interested"
    assert updated_lead.metadata["ReplyStatus"] == "Received"
    assert updated_lead.metadata["LastReplyFrom"] == "lead@example.com"


@pytest.mark.anyio
async def test_oauth_credentials_are_tenant_isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    db = build_memory_session()
    monkeypatch.setattr("app.configs.settings.settings.secret_encryption_key", "test-key")
    tenant_one = TenantContext(tenant_id="tenant-one")
    tenant_two = TenantContext(tenant_id="tenant-two")
    db.tenants.save(Tenant(tenant_id=tenant_one.tenant_id, name="One", slug="one"))
    db.tenants.save(Tenant(tenant_id=tenant_two.tenant_id, name="Two", slug="two"))
    service = ProviderCredentialService(db)
    await service.save_gmail_credentials(tenant_one, {"refresh_token": "tenant-one-token", "client_id": "a", "client_secret": "b"})
    await service.save_gmail_credentials(tenant_two, {"refresh_token": "tenant-two-token", "client_id": "c", "client_secret": "d"})

    first = await service.get_gmail_credentials(tenant_one)
    second = await service.get_gmail_credentials(tenant_two)
    tenant_one_record = db.tenants.list(tenant_one.tenant_id)[0]

    assert first["refresh_token"] == "tenant-one-token"
    assert second["refresh_token"] == "tenant-two-token"
    assert tenant_one_record.settings["providers"]["gmail"]["refresh_token_encrypted"] != "tenant-one-token"


class _FlakyAgent(BaseAgent):
    name = "flaky"

    def __init__(self) -> None:
        self.calls = 0

    async def run(self, request: AgentRequest, db) -> dict[str, object]:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("transient failure")
        return {"ok": True, "tenant_id": request.tenant.tenant_id}


@pytest.mark.anyio
async def test_job_queue_retries_and_recovers_for_same_tenant() -> None:
    db = build_memory_session()
    tenant = TenantContext(tenant_id="tenant-jobs")
    db.tenants.save(Tenant(tenant_id=tenant.tenant_id, name="Jobs", slug="jobs"))
    registry = AgentRegistry()
    flaky = _FlakyAgent()
    registry.register(flaky)
    queue = AsyncJobQueue(db=db, agents=registry)
    job = await JobService(db).enqueue(tenant, name="flaky", payload={"max_attempts": 2})

    first = await queue.run_once_for_tenant(tenant)
    after_first = db.for_tenant(tenant).get("jobs", job.id)
    second = await queue.run_once_for_tenant(tenant)
    after_second = db.for_tenant(tenant).get("jobs", job.id)

    assert first["status"] == "queued"
    assert after_first.attempt_count == 1
    assert second["status"] == "completed"
    assert after_second.attempt_count == 2
    assert after_second.tenant_id == tenant.tenant_id
