"""Sequential voice outreach agent for queued Vapi calls."""

from __future__ import annotations

import logging
from typing import Any, Dict

from app.agents.base import AgentRequest, BaseAgent
from app.core.models import VoiceCall
from app.db.session import AsyncDatabaseSession, DatabaseSession
from app.providers.vapi.client import VapiCallError, VapiClient, VapiConfigurationError
from app.services._async import maybe_await


LOGGER = logging.getLogger(__name__)
MAX_VOICE_OUTREACH_CALLS = 5


class VoiceOutreachAgent(BaseAgent):
    name = "voice_outreach"

    async def run(self, request: AgentRequest, db: DatabaseSession | AsyncDatabaseSession) -> Dict[str, Any]:
        lead_ids = _normalize_lead_ids(request.payload.get("lead_ids"))
        max_calls = _normalize_max_calls(request.payload.get("max_calls", MAX_VOICE_OUTREACH_CALLS))
        selected_lead_ids = lead_ids[:max_calls]
        results: list[Dict[str, Any]] = []
        started = 0
        skipped = 0
        failed = 0

        for lead_id in selected_lead_ids:
            result = await self._call_lead(request, db, lead_id)
            results.append(result)
            status = result["status"]
            if status == "active":
                started += 1
            elif status == "skipped":
                skipped += 1
            elif status == "failed":
                failed += 1

        return {
            "status": "completed",
            "agent_name": self.name,
            "requested": len(lead_ids),
            "processed": len(selected_lead_ids),
            "started": started,
            "skipped": skipped,
            "failed": failed,
            "results": results,
        }

    async def _call_lead(
        self,
        request: AgentRequest,
        db: DatabaseSession | AsyncDatabaseSession,
        lead_id: str,
    ) -> Dict[str, Any]:
        tenant = request.tenant
        lead = await maybe_await(db.for_tenant(tenant).get("leads", lead_id))
        if lead is None:
            return {"lead_id": lead_id, "status": "skipped", "reason": "lead_not_found"}
        phone_number = str(getattr(lead, "phone", "") or "").strip()
        if not phone_number:
            return {"lead_id": lead.id, "status": "skipped", "reason": "missing_phone"}

        voice_call = VoiceCall(
            tenant_id=tenant.tenant_id,
            lead_id=lead.id,
            user_id=tenant.user_id,
            phone_number=phone_number,
            status="pending",
            metadata={"lead_id": lead.id, "provider": "vapi", "source": self.name},
        )
        voice_call = await maybe_await(db.for_tenant(tenant).save("voice_calls", voice_call))
        try:
            response = await VapiClient().create_call(
                phone_number=phone_number,
                lead_id=lead.id,
                metadata={"tenant_id": tenant.tenant_id, "lead_id": lead.id, "voice_call_id": voice_call.id},
            )
        except VapiConfigurationError:
            return await self._mark_failed(db, tenant.tenant_id, voice_call, "vapi_not_configured", "Voice provider is not configured.")
        except VapiCallError:
            return await self._mark_failed(db, tenant.tenant_id, voice_call, "vapi_call_failed", "Voice provider call failed.")

        vapi_call_id = str(response.get("id") or response.get("callId") or response.get("call_id") or "").strip()
        if not vapi_call_id:
            return await self._mark_failed(db, tenant.tenant_id, voice_call, "missing_vapi_call_id", "Vapi did not return a call id.")
        voice_call.provider_call_id = vapi_call_id
        voice_call.status = "active"
        voice_call.metadata = {**voice_call.metadata, "vapi_response_status": str(response.get("status", "") or "")}
        voice_call = await maybe_await(db.for_tenant(tenant.tenant_id).save("voice_calls", voice_call))
        LOGGER.info(
            "voice_outreach_call_started tenant_id=%s lead_id=%s vapi_call_id=%s",
            tenant.tenant_id,
            lead.id,
            voice_call.provider_call_id,
        )
        return {"lead_id": lead.id, "status": "active", "call_id": voice_call.id, "vapi_call_id": voice_call.provider_call_id}

    async def _mark_failed(
        self,
        db: DatabaseSession | AsyncDatabaseSession,
        tenant_id: str,
        voice_call: VoiceCall,
        reason: str,
        summary: str,
    ) -> Dict[str, Any]:
        voice_call.status = "failed"
        voice_call.summary = summary
        voice_call.metadata = {**voice_call.metadata, "error": reason}
        voice_call = await maybe_await(db.for_tenant(tenant_id).save("voice_calls", voice_call))
        LOGGER.warning(
            "voice_outreach_call_failed tenant_id=%s lead_id=%s call_id=%s error=%s",
            tenant_id,
            voice_call.lead_id,
            voice_call.id,
            summary,
        )
        return {"lead_id": voice_call.lead_id, "status": "failed", "call_id": voice_call.id, "reason": reason}


def _normalize_lead_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    seen: set[str] = set()
    lead_ids: list[str] = []
    for item in value:
        lead_id = str(item or "").strip()
        if lead_id and lead_id not in seen:
            seen.add(lead_id)
            lead_ids.append(lead_id)
    return lead_ids


def _normalize_max_calls(value: Any) -> int:
    try:
        max_calls = int(value)
    except (TypeError, ValueError):
        max_calls = MAX_VOICE_OUTREACH_CALLS
    return max(1, min(MAX_VOICE_OUTREACH_CALLS, max_calls))
