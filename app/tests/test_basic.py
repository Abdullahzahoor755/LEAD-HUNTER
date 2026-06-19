from __future__ import annotations

import logging

import httpx
import pytest

from app.api.app import create_fastapi_app
from app.core.models import Lead, TenantContext, VoiceCall
from app.db.session import build_memory_session
from app.providers.vapi.client import VapiCallError


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _signup(client: httpx.AsyncClient, tenant_id: str) -> dict[str, str]:
    response = await client.post(
        "/signup",
        json={
            "tenant_id": tenant_id,
            "tenant_name": tenant_id,
            "tenant_slug": tenant_id,
            "email": f"owner@{tenant_id}.test",
            "password": "secret123",
            "full_name": "Tenant Owner",
            "plan": "Pro",
        },
    )
    assert response.status_code == 200
    return response.json()


@pytest.mark.anyio
async def test_app_boots_without_vapi_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VAPI_API_KEY", raising=False)
    monkeypatch.delenv("VAPI_ASSISTANT_ID", raising=False)
    app = create_fastapi_app(db=build_memory_session())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.anyio
async def test_voice_agent_status_does_not_expose_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.configs import settings as settings_module
    from app.providers.vapi.client import VapiClient

    async def fake_provider_reachable(self) -> bool:
        return True

    monkeypatch.setattr(settings_module.settings, "vapi_api_key", "super-secret-vapi-key")
    monkeypatch.setattr(settings_module.settings, "vapi_assistant_id", "assistant-123")
    monkeypatch.setattr(settings_module.settings, "vapi_base_url", "https://api.vapi.ai")
    monkeypatch.setattr(VapiClient, "provider_reachable", fake_provider_reachable)
    app = create_fastapi_app(db=build_memory_session())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        auth = await _signup(client, "tenant-voice-status")
        response = await client.get("/voice/agent/status", headers=_auth_headers(auth["token"]))

    assert response.status_code == 200
    payload = response.json()
    assert payload == {
        "configured": True,
        "api_key_present": True,
        "assistant_id_present": True,
        "provider_reachable": True,
    }
    assert "api_key" not in payload
    assert "super-secret-vapi-key" not in response.text


@pytest.mark.anyio
async def test_voice_agent_status_reports_missing_config(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.configs import settings as settings_module

    monkeypatch.setattr(settings_module.settings, "vapi_api_key", "")
    monkeypatch.setattr(settings_module.settings, "vapi_assistant_id", "")
    monkeypatch.setattr(settings_module.settings, "vapi_base_url", "https://api.vapi.ai")
    app = create_fastapi_app(db=build_memory_session())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        auth = await _signup(client, "tenant-voice-status-missing")
        response = await client.get("/voice/agent/status", headers=_auth_headers(auth["token"]))

    assert response.status_code == 200
    assert response.json() == {
        "configured": False,
        "api_key_present": False,
        "assistant_id_present": False,
        "provider_reachable": False,
    }


@pytest.mark.anyio
async def test_voice_agent_status_handles_unreachable_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.configs import settings as settings_module
    from app.providers.vapi.client import VapiClient

    async def fake_provider_reachable(self) -> bool:
        return False

    monkeypatch.setattr(settings_module.settings, "vapi_api_key", "super-secret-vapi-key")
    monkeypatch.setattr(settings_module.settings, "vapi_assistant_id", "assistant-123")
    monkeypatch.setattr(settings_module.settings, "vapi_base_url", "https://api.vapi.ai")
    monkeypatch.setattr(VapiClient, "provider_reachable", fake_provider_reachable)
    app = create_fastapi_app(db=build_memory_session())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        auth = await _signup(client, "tenant-voice-status-unreachable")
        response = await client.get("/voice/agent/status", headers=_auth_headers(auth["token"]))

    assert response.status_code == 200
    assert response.json() == {
        "configured": True,
        "api_key_present": True,
        "assistant_id_present": True,
        "provider_reachable": False,
    }


@pytest.mark.anyio
async def test_vapi_webhook_is_public() -> None:
    app = create_fastapi_app(db=build_memory_session())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/voice/webhook/vapi", json={"type": "call-ended"})

    assert response.status_code == 200
    assert response.json() == {"ok": True, "ignored": True}


@pytest.mark.anyio
async def test_vapi_webhook_call_started_marks_call_active() -> None:
    db = build_memory_session()
    voice_call = db.voice_calls.save(
        VoiceCall(tenant_id="tenant-webhook-started", provider_call_id="vapi-call-started", status="pending")
    )
    app = create_fastapi_app(db=db)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/voice/webhook/vapi",
            json={"type": "call-started", "call": {"id": voice_call.provider_call_id}},
        )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "ignored": False}
    saved = db.voice_calls.get(voice_call.tenant_id, voice_call.id)
    assert saved and saved.status == "active"


@pytest.mark.anyio
async def test_vapi_webhook_call_ended_saves_transcript_duration_outcome_and_summary() -> None:
    db = build_memory_session()
    voice_call = db.voice_calls.save(
        VoiceCall(tenant_id="tenant-webhook-ended", provider_call_id="vapi-call-ended", status="active")
    )
    app = create_fastapi_app(db=db)
    transport = httpx.ASGITransport(app=app)
    transcript = "Assistant: Hi. User: I am interested, send me details tomorrow."
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/voice/webhook/vapi",
            json={
                "message": {
                    "type": "call-ended",
                    "call": {"id": voice_call.provider_call_id},
                    "transcript": transcript,
                    "durationSeconds": 42,
                }
            },
        )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "ignored": False}
    saved = db.voice_calls.get(voice_call.tenant_id, voice_call.id)
    assert saved
    assert saved.status == "completed"
    assert saved.transcript == transcript
    assert saved.duration_seconds == 42
    assert saved.outcome == "callback"
    assert "interested" in saved.summary.lower()


@pytest.mark.anyio
async def test_vapi_webhook_unknown_call_id_is_ignored() -> None:
    app = create_fastapi_app(db=build_memory_session())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/voice/webhook/vapi", json={"type": "call-ended", "callId": "missing"})

    assert response.status_code == 200
    assert response.json() == {"ok": True, "ignored": True}


@pytest.mark.anyio
async def test_vapi_webhook_malformed_payload_is_ignored() -> None:
    app = create_fastapi_app(db=build_memory_session())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/voice/webhook/vapi", content="not-json")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "ignored": True}


@pytest.mark.anyio
async def test_vapi_webhook_classification_failure_marks_completed_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.voice_outcome_service import VoiceOutcomeService

    async def fail_classify(self, transcript: str) -> dict:
        raise RuntimeError("classifier failed")

    monkeypatch.setattr(VoiceOutcomeService, "classify", fail_classify)
    db = build_memory_session()
    voice_call = db.voice_calls.save(
        VoiceCall(tenant_id="tenant-webhook-classifier-failure", provider_call_id="vapi-call-classifier", status="active")
    )
    app = create_fastapi_app(db=db)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/voice/webhook/vapi",
            json={"type": "call-ended", "callId": voice_call.provider_call_id, "transcript": "User: Please call me back."},
        )

    assert response.status_code == 200
    saved = db.voice_calls.get(voice_call.tenant_id, voice_call.id)
    assert saved
    assert saved.status == "completed"
    assert saved.outcome == "unknown"
    assert saved.summary == "Call ended but no reliable transcript was available."


@pytest.mark.anyio
async def test_vapi_webhook_does_not_log_transcript_or_secret(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app.configs import settings as settings_module

    secret = "super-secret-vapi-key"
    transcript = f"User: interested but mentioned {secret}"
    monkeypatch.setattr(settings_module.settings, "vapi_api_key", secret)
    db = build_memory_session()
    voice_call = db.voice_calls.save(
        VoiceCall(tenant_id="tenant-webhook-log-safe", provider_call_id="vapi-call-log-safe", status="active")
    )
    app = create_fastapi_app(db=db)
    transport = httpx.ASGITransport(app=app)
    caplog.set_level(logging.INFO, logger="app.api.app")
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/voice/webhook/vapi",
            json={"type": "call-ended", "callId": voice_call.provider_call_id, "transcript": transcript},
        )

    assert response.status_code == 200
    assert transcript not in caplog.text
    assert secret not in caplog.text
    saved = db.voice_calls.get(voice_call.tenant_id, voice_call.id)
    assert saved and saved.transcript == transcript


@pytest.mark.anyio
async def test_voice_call_missing_config_does_not_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.configs import settings as settings_module

    monkeypatch.setattr(settings_module.settings, "vapi_api_key", "")
    monkeypatch.setattr(settings_module.settings, "vapi_assistant_id", "")
    db = build_memory_session()
    app = create_fastapi_app(db=db)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        auth = await _signup(client, "tenant-voice-missing-config")
        tenant = TenantContext(tenant_id=auth["tenant_id"], user_id=auth["user_id"])
        lead = db.for_tenant(tenant).save("leads", Lead(tenant_id=tenant.tenant_id, phone="+15551234567"))
        response = await client.post(f"/voice/call/{lead.id}", headers=_auth_headers(auth["token"]))

    assert response.status_code == 503
    assert response.json()["detail"] == "Voice provider is not configured yet."
    calls = db.voice_calls.list(auth["tenant_id"])
    assert len(calls) == 1
    assert calls[0].status == "failed"
    assert calls[0].provider_call_id == ""
    assert calls[0].summary == "Voice provider is not configured."


@pytest.mark.anyio
async def test_voice_call_missing_lead_returns_404() -> None:
    db = build_memory_session()
    app = create_fastapi_app(db=db)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        auth = await _signup(client, "tenant-voice-missing-lead")
        response = await client.post("/voice/call/not-a-real-lead", headers=_auth_headers(auth["token"]))

    assert response.status_code == 404
    assert response.json()["detail"] == "Lead not found."
    assert db.voice_calls.list(auth["tenant_id"]) == []


@pytest.mark.anyio
async def test_voice_call_missing_lead_phone_returns_422() -> None:
    db = build_memory_session()
    app = create_fastapi_app(db=db)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        auth = await _signup(client, "tenant-voice-no-phone")
        tenant = TenantContext(tenant_id=auth["tenant_id"], user_id=auth["user_id"])
        lead = db.for_tenant(tenant).save("leads", Lead(tenant_id=tenant.tenant_id, phone=""))
        response = await client.post(f"/voice/call/{lead.id}", headers=_auth_headers(auth["token"]))

    assert response.status_code == 422
    assert response.json()["detail"] == "Lead does not have a phone number."
    assert db.voice_calls.list(auth["tenant_id"]) == []


@pytest.mark.anyio
async def test_voice_call_success_creates_active_record(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.providers.vapi.client import VapiClient

    async def fake_create_call(self, *, phone_number: str, lead_id: str = "", metadata: dict | None = None) -> dict:
        assert phone_number == "+15551234567"
        assert lead_id
        assert metadata and metadata["voice_call_id"]
        return {"id": "vapi-call-123", "status": "queued"}

    db = build_memory_session()
    app = create_fastapi_app(db=db)
    monkeypatch.setattr(VapiClient, "create_call", fake_create_call)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        auth = await _signup(client, "tenant-voice-success")
        tenant = TenantContext(tenant_id=auth["tenant_id"], user_id=auth["user_id"])
        lead = db.for_tenant(tenant).save("leads", Lead(tenant_id=tenant.tenant_id, phone="+15551234567"))
        response = await client.post(f"/voice/call/{lead.id}", headers=_auth_headers(auth["token"]))

    assert response.status_code == 200
    payload = response.json()
    assert payload["vapi_call_id"] == "vapi-call-123"
    assert payload["status"] == "active"
    calls = db.voice_calls.list(auth["tenant_id"])
    assert len(calls) == 1
    assert calls[0].id == payload["call_id"]
    assert calls[0].provider_call_id == "vapi-call-123"
    assert calls[0].status == "active"


@pytest.mark.anyio
async def test_voice_call_vapi_failure_marks_call_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.providers.vapi.client import VapiClient

    async def fake_create_call(self, *, phone_number: str, lead_id: str = "", metadata: dict | None = None) -> dict:
        raise VapiCallError("Vapi call request failed with status 500.")

    db = build_memory_session()
    app = create_fastapi_app(db=db)
    monkeypatch.setattr(VapiClient, "create_call", fake_create_call)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        auth = await _signup(client, "tenant-voice-failure")
        tenant = TenantContext(tenant_id=auth["tenant_id"], user_id=auth["user_id"])
        lead = db.for_tenant(tenant).save("leads", Lead(tenant_id=tenant.tenant_id, phone="+15551234567"))
        response = await client.post(f"/voice/call/{lead.id}", headers=_auth_headers(auth["token"]))

    assert response.status_code == 502
    assert response.json()["detail"] == "Voice call could not be started right now."
    calls = db.voice_calls.list(auth["tenant_id"])
    assert len(calls) == 1
    assert calls[0].status == "failed"
    assert calls[0].metadata["error"] == "vapi_call_failed"
    assert calls[0].summary == "Voice provider call failed."


@pytest.mark.anyio
async def test_voice_call_logs_and_errors_do_not_expose_vapi_api_key(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app.configs import settings as settings_module
    from app.providers.vapi.client import VapiClient

    secret = "super-secret-vapi-key"

    async def fake_create_call(self, *, phone_number: str, lead_id: str = "", metadata: dict | None = None) -> dict:
        raise VapiCallError(f"Provider rejected key {secret}")

    monkeypatch.setattr(settings_module.settings, "vapi_api_key", secret)
    monkeypatch.setattr(settings_module.settings, "vapi_assistant_id", "assistant-123")
    monkeypatch.setattr(VapiClient, "create_call", fake_create_call)
    db = build_memory_session()
    app = create_fastapi_app(db=db)
    transport = httpx.ASGITransport(app=app)
    caplog.set_level(logging.INFO, logger="app.api.app")
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        auth = await _signup(client, "tenant-voice-secret-safe")
        tenant = TenantContext(tenant_id=auth["tenant_id"], user_id=auth["user_id"])
        lead = db.for_tenant(tenant).save("leads", Lead(tenant_id=tenant.tenant_id, phone="+15551234567"))
        response = await client.post(f"/voice/call/{lead.id}", headers=_auth_headers(auth["token"]))

    assert response.status_code == 502
    assert secret not in response.text
    assert secret not in caplog.text
    calls = db.voice_calls.list(auth["tenant_id"])
    assert len(calls) == 1
    assert calls[0].summary == "Voice provider call failed."
    assert secret not in calls[0].summary


@pytest.mark.anyio
async def test_voice_campaign_endpoint_is_pending() -> None:
    app = create_fastapi_app(db=build_memory_session())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        auth = await _signup(client, "tenant-voice-campaign-pending")
        response = await client.post("/voice/campaign", headers=_auth_headers(auth["token"]))

    assert response.status_code == 501
    assert response.json()["detail"] == "Voice calling foundation added; implementation pending."
