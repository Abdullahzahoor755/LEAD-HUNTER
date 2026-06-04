"""Lead generation agent that wraps the legacy monolith for now."""

from __future__ import annotations

import logging
import re
import traceback
from typing import Any, Dict, List

from app.agents.base import AgentRequest, BaseAgent
from app.core.auth import has_pro_features
from app.core.models import AgentRun, Lead
from app.db.session import AsyncDatabaseSession, DatabaseSession
from app.services.agent_run_service import AgentRunService
from app.services._async import maybe_await
from app.services.lead_service import LeadService

import leads as legacy_leads

LOGGER = logging.getLogger(__name__)


class LeadGenerationAgent(BaseAgent):
    name = "lead_generation"

    async def run(self, request: AgentRequest, db: DatabaseSession | AsyncDatabaseSession) -> Dict[str, Any]:
        tenant_id = request.tenant.tenant_id
        limit = int(request.payload.get("limit", legacy_leads.DEFAULT_LEAD_LIMIT))
        query = str(request.payload.get("query", "")).strip()
        target_country = self._country_from_payload(request.payload)
        lead_service = LeadService(db)
        ai_mode = "fallback" if not await self._tenant_has_pro_features(db, tenant_id) else ""
        legacy_leads.load_environment()

        try:
            if query:
                raw_leads = legacy_leads.process_query(query, seen_websites=set(), limit=limit, ai_mode=ai_mode)
            else:
                raw_leads = legacy_leads.generate_leads(limit=limit, ai_mode=ai_mode)

            saved: List[Lead] = []
            skipped_count = 0
            for item in raw_leads or []:
                decision = str(item.get("decision", "")).strip().lower()
                qualified = bool(item.get("qualified", False))
                junk_reason = self._junk_source_reason(item, query)
                if not qualified or decision == "reject":
                    skipped_count += 1
                    LOGGER.info(
                        "Skipping persistence for rejected lead tenant=%s website=%s reason=%s",
                        tenant_id,
                        item.get("website", ""),
                        item.get("skip_reason", item.get("quality_reason", "")),
                    )
                    continue
                if junk_reason:
                    skipped_count += 1
                    LOGGER.info(
                        "Skipping persistence for junk source tenant=%s website=%s reason=%s",
                        tenant_id,
                        item.get("website", ""),
                        junk_reason,
                    )
                    continue
                lead = Lead(
                    tenant_id=tenant_id,
                    company=str(item.get("company_name", "")),
                    company_url=str(item.get("website", "")),
                    country=str(item.get("country", "") or target_country),
                    verified_email=str(item.get("email", "")),
                    service_reason=self._service_reason_from_raw(item),
                    outreach_status=str(item.get("email_status", "pending")),
                    website=str(item.get("website", "")),
                    email=str(item.get("email", "")),
                    phone=str(item.get("phone", "")),
                    location=str(item.get("address", "")),
                    industry=str(item.get("industry", "")),
                    score=int(item.get("lead_score", 0) or 0),
                    reason=str(item.get("reason", "")),
                    status=str(item.get("email_status", "pending")),
                    source_query=query,
                    metadata=self._metadata_from_raw(item),
                )
                if not lead.service_reason:
                    LOGGER.info(
                        "Lead service_reason empty tenant=%s website=%s reason=missing_claude_reason_or_intent_summary",
                        tenant_id,
                        item.get("website", ""),
                    )
                if not str(item.get("industry", "")).strip():
                    LOGGER.info(
                        "Lead industry empty tenant=%s website=%s reason=missing_claude_industry",
                        tenant_id,
                        item.get("website", ""),
                    )
                saved.append(await lead_service.upsert_lead(request.tenant, lead))

            output = {"saved_leads": len(saved), "skipped_leads": skipped_count, "lead_count": len(saved), "query": query}
            agent_run = AgentRun(
                tenant_id=tenant_id,
                agent_name=self.name,
                status="completed",
                input_payload=request.payload,
                output_payload=output,
            )
            await AgentRunService(db).record_run(request.tenant, agent_run)
            return {"status": "SUCCESS", "message": "Lead generation completed.", "data": {"agent_run_id": agent_run.id, **output}}
        except Exception as error:
            LOGGER.exception("Lead generation failed for tenant=%s query=%r", tenant_id, query)
            agent_run = AgentRun(
                tenant_id=tenant_id,
                agent_name=self.name,
                status="failed",
                input_payload=request.payload,
                output_payload={},
                error=f"{error}\n{traceback.format_exc()}",
            )
            try:
                await AgentRunService(db).record_run(request.tenant, agent_run)
            except Exception:
                LOGGER.exception("Failed to persist failed agent run for tenant=%s", tenant_id)
            return {
                "status": "FAILED",
                "message": "Lead generation failed safely.",
                "data": {"saved_leads": 0, "skipped_leads": 0, "lead_count": 0, "query": query},
            }

    def _service_reason_from_raw(self, item: Dict[str, Any]) -> str:
        analysis_reason = str(item.get("analysis_reason", "")).strip()
        if analysis_reason and not self._looks_like_score_breakdown(analysis_reason):
            return analysis_reason
        intent_summary = str(item.get("intent_summary", "")).strip()
        return "" if self._looks_like_score_breakdown(intent_summary) else intent_summary

    def _country_from_payload(self, payload: Dict[str, Any]) -> str:
        explicit_country = str(payload.get("country", "") or payload.get("target_country", "")).strip()
        detected = self._normalize_country(explicit_country)
        if detected:
            return detected
        return self._normalize_country(str(payload.get("query", "")).strip())

    def _normalize_country(self, value: str) -> str:
        lowered = str(value or "").lower()
        for alias, canonical in LeadService.COUNTRY_ALIASES.items():
            if re.search(rf"\b{re.escape(alias)}\b", lowered):
                return canonical
        return ""

    def _metadata_from_raw(self, item: Dict[str, Any]) -> Dict[str, Any]:
        metadata = dict(item)
        if item.get("ai_mode"):
            metadata["ai_mode"] = str(item.get("ai_mode", "")).strip()
        score_breakdown = str(item.get("score_breakdown", "") or item.get("reason", "")).strip()
        if score_breakdown and self._looks_like_score_breakdown(score_breakdown):
            metadata["score_breakdown"] = score_breakdown
        if item.get("quality_reason"):
            metadata["quality_reason"] = str(item.get("quality_reason", "")).strip()
        return metadata

    async def _tenant_has_pro_features(self, db: DatabaseSession | AsyncDatabaseSession, tenant_id: str) -> bool:
        tenants = await maybe_await(db.tenants.list(tenant_id))
        plan = str(tenants[0].subscription_plan if tenants else "").strip()
        return has_pro_features(plan)

    def _looks_like_score_breakdown(self, value: str) -> bool:
        lowered = str(value or "").lower()
        return any(marker in lowered for marker in ("email=40/40", "phone=25/25", "relevance=", "quality="))

    def _junk_source_reason(self, item: Dict[str, Any], query: str) -> str:
        query_text = str(query or "").lower()
        allowed_terms = ("directory", "job board", "jobs", "employment", "data broker", "email list", "aggregator")
        if any(term in query_text for term in allowed_terms):
            return ""

        website = str(item.get("website", "")).lower()
        industry = str(item.get("industry", "")).lower()
        company_name = str(item.get("company_name", "")).lower()
        summary = str(item.get("company_summary", "")).lower()
        quality_reason = str(item.get("quality_reason", "")).lower()
        category = str(item.get("quality_category", "")).lower()
        combined = " ".join([website, industry, company_name, summary, quality_reason, category])

        domain_markers = (
            "the-saudi.net",
            "smergers.com",
            "sa.mustakbil.com",
            "reachgulfbusiness.com",
        )
        if any(marker in website for marker in domain_markers):
            return "known_junk_source_domain"

        keyword_markers = (
            "directory",
            "business directory",
            "job board",
            "employment platform",
            "aggregator",
            "data broker",
            "email list provider",
            "email list",
            "database vendor",
            "lead database",
            "non-profit network",
        )
        for marker in keyword_markers:
            if marker in combined:
                return marker.replace(" ", "_")
        return ""
