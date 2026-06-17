"""Lead generation agent that wraps the legacy monolith for now."""

from __future__ import annotations

import logging
import os
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
        job_id = str(request.payload.get("_job_id", "") or "").strip()
        limit = int(request.payload.get("limit", legacy_leads.DEFAULT_LEAD_LIMIT))
        query = str(request.payload.get("query", "")).strip()
        niche = str(request.payload.get("niche", "") or request.payload.get("industry", "") or "").strip()
        location = str(request.payload.get("location", "") or request.payload.get("country", "") or request.payload.get("target_country", "") or "").strip()
        if niche or location:
            query = legacy_leads.build_search_query(niche=niche, location=location, query=query)
        target_country = self._country_from_payload(request.payload)
        lead_service = LeadService(db)
        ai_mode = "fallback" if not await self._tenant_has_pro_features(db, tenant_id) else ""
        legacy_leads.load_environment()
        missing_keys = self._missing_required_keys()
        if missing_keys:
            output = self._empty_output(
                query=query,
                message="Lead search is not configured. Add SERPER_API_KEY to .env and restart the backend.",
                rejection_reasons={"missing_search_api_key": 1},
            )
            LOGGER.error(
                "Lead generation preflight failed tenant=%s job_query_present=%s missing_keys=%s",
                tenant_id,
                bool(query),
                ",".join(missing_keys),
            )
            await AgentRunService(db).record_run(
                request.tenant,
                AgentRun(
                    tenant_id=tenant_id,
                    agent_name=self.name,
                    status="failed",
                    input_payload=request.payload,
                    output_payload=output,
                    error=output["message"],
                ),
            )
            return {"status": "FAILED", "message": output["message"], "data": output}

        try:
            if query:
                try:
                    raw_leads = legacy_leads.process_query(
                        query,
                        seen_websites=set(),
                        limit=limit,
                        ai_mode=ai_mode,
                        niche=niche,
                        location=location,
                    )
                except TypeError as error:
                    if "unexpected keyword argument" not in str(error):
                        raise
                    raw_leads = legacy_leads.process_query(query, seen_websites=set(), limit=limit, ai_mode=ai_mode)
            else:
                raw_leads = legacy_leads.generate_leads(limit=limit, ai_mode=ai_mode)
            pipeline_stats = self._pipeline_stats(raw_leads)
            pipeline_events = self._pipeline_events()
            await self._record_job_progress(db, request.tenant, job_id, pipeline_stats, pipeline_events)
            LOGGER.info(
                "Lead pipeline counts tenant=%s job_query_present=%s discovered_urls_count=%s scraped_pages_count=%s "
                "cleaned_records_count=%s extracted_emails_count=%s scored_leads_count=%s accepted_leads_count=%s "
                "rejected_leads_count=%s",
                tenant_id,
                bool(query),
                pipeline_stats["discovered_urls_count"],
                pipeline_stats["scraped_pages_count"],
                pipeline_stats["cleaned_records_count"],
                pipeline_stats["extracted_emails_count"],
                pipeline_stats["scored_leads_count"],
                pipeline_stats["accepted_leads_count"],
                pipeline_stats["rejected_leads_count"],
            )

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
                    job_id=job_id,
                    company=str(item.get("company_name", "")),
                    company_url=str(item.get("website", "")),
                    country=str(item.get("country", "") or target_country),
                    verified_email=str(item.get("email", "")),
                    service_reason=self._service_reason_from_raw(item),
                    outreach_status="pending",
                    website=str(item.get("website", "")),
                    email=str(item.get("email", "")),
                    phone=str(item.get("phone", "")),
                    location=str(item.get("address", "")),
                    industry=str(item.get("industry", "")),
                    score=int(item.get("lead_score", 0) or 0),
                    reason=str(item.get("reason", "")),
                    status="pending",
                    source_query=query,
                    metadata=self._metadata_from_raw(item),
                )
                lead.metadata["lead_quality_grade"] = legacy_leads.lead_quality_grade(
                    lead.website or lead.company_url,
                    lead.metadata,
                    email=lead.verified_email or lead.email,
                    phone=lead.phone,
                )
                if not (lead.verified_email or lead.email):
                    lead.metadata["outreach_readiness"] = "needs_manual_contact"
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
                saved_lead = await lead_service.upsert_lead(request.tenant, lead)
                saved.append(saved_lead)
                pipeline_events.append(
                    {
                        "stage": "saving",
                        "status": "saved",
                        "domain": legacy_leads.root_domain_from_url(saved_lead.company_url or saved_lead.website),
                        "url": saved_lead.company_url or saved_lead.website,
                        "message": "lead_saved",
                        "metadata": {
                            "quality_grade": saved_lead.metadata.get("lead_quality_grade", ""),
                            "outreach_readiness": "email_ready" if saved_lead.verified_email else "needs_manual_contact",
                        },
                    }
                )

            output = {
                **pipeline_stats,
                "saved_leads": len(saved),
                "skipped_leads": skipped_count,
                "lead_count": len(saved),
                "query": query,
                "message": self._result_message(len(saved), pipeline_stats),
            }
            output["a_grade_leads"] = sum(1 for item in saved if str(item.metadata.get("lead_quality_grade", "")) == "A")
            output["b_grade_leads"] = sum(1 for item in saved if str(item.metadata.get("lead_quality_grade", "")) == "B")
            output["no_email_leads"] = sum(1 for item in saved if not item.verified_email)
            output["outreach_ready_leads"] = sum(1 for item in saved if item.verified_email)
            await self._record_job_progress(db, request.tenant, job_id, output, pipeline_events, completed=True)
            agent_run = AgentRun(
                tenant_id=tenant_id,
                agent_name=self.name,
                status="completed",
                input_payload=request.payload,
                output_payload=output,
            )
            await AgentRunService(db).record_run(request.tenant, agent_run)
            return {"status": "SUCCESS", "message": output["message"], "data": {"agent_run_id": agent_run.id, **output}}
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
                "data": self._empty_output(query=query, message="Lead generation failed safely."),
            }

    def _missing_required_keys(self) -> list[str]:
        if os.getenv("SERPER_API_KEY", "").strip():
            return []
        if getattr(legacy_leads.search_google, "__module__", "") != "leads":
            return []
        if getattr(legacy_leads.process_query, "__module__", "") != "leads":
            return []
        return ["SERPER_API_KEY"]

    def _pipeline_stats(self, raw_leads: List[Dict[str, Any]] | None) -> Dict[str, Any]:
        stats = dict(getattr(legacy_leads, "LAST_PIPELINE_STATS", {}) or {})
        defaults = self._empty_output(query="", message="")
        for key in (
            "discovered_urls_count",
            "query_variants_count",
            "raw_results_count",
            "filtered_results_count",
            "rejected_bad_domain_count",
            "rejected_irrelevant_count",
            "duplicate_domain_count",
            "scraped_pages_count",
            "cleaned_records_count",
            "extracted_emails_count",
            "scored_leads_count",
            "accepted_leads_count",
            "rejected_leads_count",
        ):
            stats[key] = int(stats.get(key, 0) or 0)
        if not stats["accepted_leads_count"] and raw_leads:
            stats["accepted_leads_count"] = len(raw_leads)
            stats["scored_leads_count"] = max(stats["scored_leads_count"], len(raw_leads))
            stats["extracted_emails_count"] = max(
                stats["extracted_emails_count"],
                sum(1 for item in raw_leads if str(item.get("email", "")).strip()),
            )
        reasons = stats.get("rejection_reasons", {})
        stats["rejection_reasons"] = dict(reasons) if isinstance(reasons, dict) else {}
        for key, value in defaults.items():
            stats.setdefault(key, value)
        return stats

    def _empty_output(
        self,
        query: str,
        message: str,
        rejection_reasons: Dict[str, int] | None = None,
    ) -> Dict[str, Any]:
        return {
            "discovered_urls_count": 0,
            "final_query": query,
            "query_variants_count": 0,
            "raw_results_count": 0,
            "filtered_results_count": 0,
            "rejected_bad_domain_count": 0,
            "rejected_irrelevant_count": 0,
            "duplicate_domain_count": 0,
            "scraped_pages_count": 0,
            "cleaned_records_count": 0,
            "extracted_emails_count": 0,
            "scored_leads_count": 0,
            "accepted_leads_count": 0,
            "rejected_leads_count": 0,
            "saved_leads": 0,
            "skipped_leads": 0,
            "lead_count": 0,
            "query": query,
            "rejection_reasons": dict(rejection_reasons or {}),
            "message": message,
        }

    def _result_message(self, saved_count: int, stats: Dict[str, Any]) -> str:
        if saved_count:
            return f"Lead generation completed. Saved {saved_count} lead(s)."
        if int(stats.get("discovered_urls_count", 0) or 0) == 0:
            return "Lead generation completed with 0 leads: no URLs were discovered."
        return "Lead generation completed with 0 leads: all candidates were rejected or already existed."

    def _service_reason_from_raw(self, item: Dict[str, Any]) -> str:
        quality_filter = item.get("quality_filter", {}) if isinstance(item.get("quality_filter", {}), dict) else {}
        candidates = (
            item.get("reason", ""),
            quality_filter.get("reason", ""),
            item.get("quality_reason", ""),
            item.get("analysis_reason", ""),
            item.get("intent_summary", ""),
        )
        for value in candidates:
            reason = str(value or "").strip()
            if self._usable_service_reason(reason):
                return reason
        return self._fallback_service_reason(item)

    def _usable_service_reason(self, value: str) -> bool:
        return bool(value) and not self._looks_like_score_breakdown(value) and not self._contains_generic_reason(value)

    def _contains_generic_reason(self, value: str) -> bool:
        lowered = str(value or "").lower()
        banned = (
            "matches the search query",
            "target from the search query",
            "target from search query",
            "workflow automation may help",
            "may need workflow automation",
            "search query",
        )
        return any(phrase in lowered for phrase in banned)

    def _fallback_service_reason(self, item: Dict[str, Any]) -> str:
        company = str(item.get("company_name", "") or item.get("company", "")).strip()
        website = str(item.get("website", "") or item.get("company_url", "")).strip()
        industry = str(item.get("industry", "")).strip()
        subject = company or legacy_leads.root_domain_from_url(website) or "This company"
        if industry and industry.lower() not in {"other", "unknown", "n/a", "na", "none"}:
            return f"{subject} appears relevant for outreach based on its {industry} business profile and contact availability."
        return f"{subject} appears relevant for outreach based on its business profile and contact availability."

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
        if item.get("email_status"):
            metadata["raw_email_status"] = str(item.get("email_status", "")).strip()
        if item.get("ai_mode"):
            metadata["ai_mode"] = str(item.get("ai_mode", "")).strip()
        score_breakdown = str(item.get("score_breakdown", "") or item.get("reason", "")).strip()
        if score_breakdown and self._looks_like_score_breakdown(score_breakdown):
            metadata["score_breakdown"] = score_breakdown
        if item.get("quality_reason"):
            metadata["quality_reason"] = str(item.get("quality_reason", "")).strip()
        return metadata

    def _pipeline_events(self) -> list[Dict[str, Any]]:
        return list(getattr(legacy_leads, "LAST_PIPELINE_EVENTS", []) or [])

    async def _record_job_progress(
        self,
        db: DatabaseSession | AsyncDatabaseSession,
        tenant,
        job_id: str,
        stats: Dict[str, Any],
        events: list[Dict[str, Any]],
        completed: bool = False,
    ) -> None:
        if not job_id:
            return
        scoped = db.for_tenant(tenant)
        job = await maybe_await(scoped.get("jobs", job_id))
        if job is None:
            return
        safe_events: list[Dict[str, Any]] = []
        for event in events[-120:]:
            payload = dict(event or {})
            payload.pop("content", None)
            payload.pop("html", None)
            safe_events.append(payload)
        summary = dict(job.result_summary or {})
        summary.update(
            {
                "current_stage": "completed" if completed else "running",
                "progress_percentage": 100 if completed else min(95, int(stats.get("scraped_pages_count", 0) or 0) * 5),
                "stats": dict(stats or {}),
                "events": safe_events,
            }
        )
        job.result_summary = summary
        await maybe_await(scoped.save("jobs", job))

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
