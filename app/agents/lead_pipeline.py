"""JSON-in/JSON-out agents for the legacy lead generation pipeline."""

from __future__ import annotations

import json
import asyncio
import re
from datetime import timedelta
from typing import Any, Dict


class JsonAgent:
    """Small agent contract for the legacy pipeline."""

    name = "JsonAgent"

    def run(self, input_json: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(input_json, dict):
            raise TypeError(f"{self.name} expects input_json to be a dict.")
        output = self._run(input_json)
        output.setdefault("agent", self.name)
        self.log_output(output)
        return output

    def _run(self, input_json: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    def log_output(self, output: Dict[str, Any]) -> None:
        import leads as legacy

        try:
            serialized = json.dumps(output, default=str, ensure_ascii=True)
        except TypeError:
            serialized = str(output)
        if len(serialized) > 2500:
            serialized = f"{serialized[:2500]}...<truncated>"
        legacy.LOGGER.info("%s output: %s", self.name, serialized)


class DiscoveryAgent(JsonAgent):
    name = "DiscoveryAgent"

    def _run(self, input_json: Dict[str, Any]) -> Dict[str, Any]:
        import leads as legacy

        query = str(input_json.get("query", "")).strip()
        niche = str(input_json.get("niche", "") or input_json.get("industry", "") or "").strip()
        location = str(input_json.get("location", "") or input_json.get("country", "") or input_json.get("target_country", "") or "").strip()
        query_variants = legacy.build_search_query_variants(niche=niche, location=location, query=query)
        if not query_variants:
            raise ValueError("DiscoveryAgent requires a non-empty 'query'.")
        limit = input_json.get("limit")
        limit_value = int(limit) if limit is not None else None
        raw_result_limit = min(max((limit_value or 10) * 6, 30), 50)
        candidate_limit = min(max((limit_value or 10) * 4, 20), 50)
        seen_websites = {str(item) for item in input_json.get("seen_websites", []) if str(item).strip()}

        all_results = []
        aggregate_stats = {
            "raw_results_count": 0,
            "filtered_results_count": 0,
            "rejected_bad_domain_count": 0,
            "rejected_irrelevant_count": 0,
            "duplicate_domain_count": 0,
        }
        events = []
        candidate_websites = []
        for variant in query_variants:
            try:
                search_results = legacy.search_google(variant, num=raw_result_limit)
            except TypeError:
                search_results = legacy.search_google(variant)
            all_results.extend(search_results)
            websites, stats = legacy.extract_websites_with_stats(search_results)
            for key, value in stats.items():
                if key == "events":
                    events.extend(list(value or []))
                    continue
                aggregate_stats[key] += int(value or 0)
            for website in websites:
                website_key = legacy.get_website_key(website)
                if website_key in seen_websites:
                    aggregate_stats["duplicate_domain_count"] += 1
                    legacy.LOGGER.info("Skipping duplicate website already processed: %s", website)
                    events.append({"stage": "filtering", "status": "rejected", "domain": legacy.root_domain_from_url(website), "url": website, "reason": "rejected_duplicate_domain", "message": "duplicate_domain_skipped"})
                    continue
                seen_websites.add(website_key)
                candidate_websites.append(website)
                if len(candidate_websites) >= candidate_limit:
                    break
            if len(candidate_websites) >= candidate_limit:
                break

        return {
            "query": query_variants[0],
            "final_query": query_variants[0],
            "query_variants": query_variants,
            "query_variants_count": len(query_variants),
            "search_results_count": len(all_results),
            **aggregate_stats,
            "websites": candidate_websites,
            "website_count": len(candidate_websites),
            "seen_websites": sorted(seen_websites),
            "events": events,
        }


class ScraperAgent(JsonAgent):
    name = "ScraperAgent"

    def _run(self, input_json: Dict[str, Any]) -> Dict[str, Any]:
        import leads as legacy

        website = str(input_json.get("website", "")).strip()
        if not website:
            raise ValueError("ScraperAgent requires a non-empty 'website'.")

        contact_context = legacy.CrawlContext()
        scrape_context = legacy.CrawlContext()
        contact_info = legacy.extract_contact_info(website, context=contact_context)
        website_text, scraping_method, fallback_reason = legacy.scrape_website(website, context=scrape_context)
        lead_status = "Pending"
        if not website_text:
            lead_status = "no_content_scraped"
        if scrape_context.last_status in ("blocked_site", "slow_site", "js_site"):
            lead_status = scrape_context.last_status

        return {
            "website": website,
            "website_text": website_text,
            "website_text_length": len(website_text),
            "contact_info": contact_info,
            "scraping_method": scraping_method,
            "fallback_reason": fallback_reason,
            "contact_status": contact_context.last_status,
            "contact_failure_reason": str(contact_info.get("failure_reason", "")),
            "scrape_status": scrape_context.last_status or ("empty_text" if not website_text else "ok"),
            "lead_status": lead_status,
            "crawl_metrics": {
                key: int(contact_context.metrics.get(key, 0)) + int(scrape_context.metrics.get(key, 0))
                for key in contact_context.metrics
            },
        }


class CleaningAgent(JsonAgent):
    name = "CleaningAgent"

    def _run(self, input_json: Dict[str, Any]) -> Dict[str, Any]:
        import leads as legacy

        website = str(input_json.get("website", "")).strip()
        contact_info = dict(input_json.get("contact_info", {}) or {})
        website_text = str(input_json.get("website_text", "") or "")
        cleaned_text = re.sub(r"\s+", " ", website_text).strip()[:3000]
        company_name = str(contact_info.get("company_name", "")).strip() or legacy.get_website_key(website)

        return {
            "website": website,
            "company_name": company_name,
            "email": str(contact_info.get("email", "")).strip().lower(),
            "phone": str(contact_info.get("phone", "")).strip(),
            "address": str(contact_info.get("address", "")).strip(),
            "contact_page": str(contact_info.get("contact_page", "")).strip(),
            "email_candidates": list(contact_info.get("email_candidates", []) or []),
            "candidate_emails": list(contact_info.get("candidate_emails", []) or []),
            "likely_email": str(contact_info.get("likely_email", "")).strip().lower(),
            "likely_emails": list(contact_info.get("likely_emails", []) or []),
            "email_confidence": str(contact_info.get("email_confidence", "")).strip(),
            "lead_readiness_score": int(contact_info.get("lead_readiness_score", 0) or 0),
            "website_text": cleaned_text,
            "website_text_length": len(cleaned_text),
            "lead_status": str(input_json.get("lead_status", "")).strip() or "Pending",
            "scraping_method": str(input_json.get("scraping_method", "")).strip(),
            "fallback_reason": str(input_json.get("fallback_reason", "")).strip(),
            "scrape_status": str(input_json.get("scrape_status", "")).strip(),
            "crawl_metrics": dict(input_json.get("crawl_metrics", {}) or {}),
        }


class ScoringAgent(JsonAgent):
    name = "ScoringAgent"

    INDUSTRY_KEYWORDS = {
        "Logistics": ("logistics", "freight", "shipping", "transport", "warehouse", "supply chain"),
        "Healthcare": ("healthcare", "clinic", "hospital", "medical", "pharma", "dental"),
        "Education": ("education", "school", "university", "academy", "learning", "training"),
        "Real Estate": ("real estate", "property", "construction", "developer", "brokerage"),
        "Financial Services": ("finance", "financial", "bank", "insurance", "fintech", "accounting"),
        "Retail & Ecommerce": ("retail", "ecommerce", "e-commerce", "shop", "store", "apparel", "fashion"),
        "Technology": ("software", "technology", "saas", "it", "cloud", "cybersecurity", "automation"),
        "Manufacturing": ("manufacturing", "factory", "industrial", "machinery", "production"),
        "Hospitality": ("hotel", "restaurant", "hospitality", "travel", "tourism"),
        "Professional Services": ("consulting", "agency", "legal", "professional services", "services"),
    }

    def _run(self, input_json: Dict[str, Any]) -> Dict[str, Any]:
        import leads as legacy

        website = str(input_json.get("website", "")).strip()
        website_text = str(input_json.get("website_text", "") or "")
        lead_status = str(input_json.get("lead_status", "")).strip()
        contact_info = {
            "company_name": str(input_json.get("company_name", "")).strip(),
            "email": str(input_json.get("email", "")).strip(),
            "phone": str(input_json.get("phone", "")).strip(),
            "address": str(input_json.get("address", "")).strip(),
            "contact_page": str(input_json.get("contact_page", "")).strip(),
            "email_candidates": list(input_json.get("email_candidates", []) or []),
            "candidate_emails": list(input_json.get("candidate_emails", []) or []),
            "likely_email": str(input_json.get("likely_email", "")).strip().lower(),
            "likely_emails": list(input_json.get("likely_emails", []) or []),
            "email_confidence": str(input_json.get("email_confidence", "")).strip(),
            "lead_readiness_score": int(input_json.get("lead_readiness_score", 0) or 0),
        }
        original_phone = contact_info["phone"]
        contact_info["phone"] = legacy.normalize_phone(original_phone)
        scraped_email_present = legacy.is_valid_email(contact_info["email"])
        scraped_email_invalid = bool(contact_info["email"] and not scraped_email_present)
        scraped_phone_present = bool(contact_info["phone"])
        safety_notes: list[str] = []
        if original_phone and not contact_info["phone"]:
            safety_notes.append("invalid phone format removed (failed E.164 validation)")
        query = str(input_json.get("query", "")).strip()
        forced_ai_mode = str(input_json.get("ai_mode", "")).strip().lower()
        analysis: Dict[str, Any] = {}
        ai_mode = "provider"
        ai_runtime = dict(input_json.get("_ai_runtime", {}) or {})
        if website_text and forced_ai_mode != "fallback":
            provider_payload = self._build_provider_payload(input_json)
            ai_runtime["_ai_payload_truncated"] = self._provider_payload_was_truncated(input_json)
            analysis, ai_mode = self._provider_analysis(provider_payload, ai_runtime)
        if forced_ai_mode == "fallback" or not analysis:
            ai_mode = "fallback"
            if forced_ai_mode == "fallback":
                legacy.LOGGER.info("AI scoring skipped provider=fallback reason=plan_or_explicit_fallback website=%s", website)

            safe_name, name_note = self._validated_company_name(contact_info["company_name"], website)
            contact_info["company_name"] = safe_name
            notes = [*safety_notes, *[note for note in [name_note] if note]]

            generic_email = legacy.is_generic_email_address(contact_info["email"])
            phone_only = bool(generic_email and contact_info["phone"])
            if phone_only:
                notes.append("generic email — phone channel only")
            elif generic_email:
                notes.append("generic email — deprioritized; no valid phone channel")
            safety_notes = notes
            analysis = self._fallback_analysis(
                query=query,
                website_text=website_text,
                company_name=contact_info["company_name"],
                notes=notes,
            )
            analysis["generic_email"] = generic_email
            analysis["phone_only_eligible"] = phone_only
        analysis["ai_mode"] = ai_mode
        # Provider relevance is authoritative when it positively identifies a fit.
        analysis["relevance_passed"] = bool(
            analysis.get("relevance_passed", False) or analysis.get("needs_it_services", False)
        )
        extracted_email = str(analysis.get("extracted_email", "")).strip().lower()
        if not contact_info["email"] and extracted_email and legacy.is_valid_email(extracted_email):
            contact_info["email"] = extracted_email
        if extracted_email and legacy.is_valid_email(extracted_email) and extracted_email not in contact_info["candidate_emails"]:
            contact_info["candidate_emails"].append(extracted_email)
            contact_info["email_candidates"].append(
                {"email": extracted_email, "source": "ai_extracted", "page_url": website, "confidence": "verified_email"}
            )
        generic_email = legacy.is_generic_email_address(contact_info["email"])
        phone_only = bool(generic_email and contact_info["phone"])
        analysis["generic_email"] = generic_email
        analysis["phone_only_eligible"] = phone_only
        if ai_mode != "fallback":
            if phone_only:
                safety_notes.append("generic email — phone channel only")
            elif generic_email:
                safety_notes.append("generic email — deprioritized; no valid phone channel")
        scored = legacy.score_lead(
            website=website,
            contact_info=contact_info,
            analysis=analysis,
            website_text=website_text,
            lead_status=lead_status,
        )
        quality_filter = legacy.apply_lead_quality_filter(
            website=website,
            website_text=website_text,
            company_name=contact_info["company_name"],
            contact_info=contact_info,
        )
        intent_analysis = analysis.get("intent_analysis", {}) if isinstance(analysis.get("intent_analysis", {}), dict) else {}

        reason = quality_filter["reason"]
        score_breakdown = scored["score_breakdown"]
        fallback_reason = str(input_json.get("fallback_reason", "")).strip()
        if fallback_reason:
            score_breakdown = f"{score_breakdown} | fallback_reason={fallback_reason}"

        lead = {
            "website": website,
            "company_name": contact_info["company_name"] or legacy.get_website_key(website),
            "email": contact_info["email"],
            "phone": contact_info["phone"],
            "address": contact_info["address"],
            "contact_page": contact_info["contact_page"],
            "email_candidates": contact_info["email_candidates"],
            "candidate_emails": contact_info["candidate_emails"],
            "likely_email": contact_info["likely_email"],
            "likely_emails": contact_info["likely_emails"],
            "email_confidence": contact_info["email_confidence"],
            "lead_readiness_score": contact_info["lead_readiness_score"],
            "industry": str(analysis.get("industry", "")).strip(),
            "company_summary": str(analysis.get("company_summary", "")).strip(),
            "needs_it_services": bool(analysis.get("needs_it_services", False)),
            "relevance_passed": bool(analysis.get("relevance_passed", False)),
            "lead_score": scored["lead_score"],
            "reason": reason,
            "analysis_reason": str(analysis.get("reason", "")).strip(),
            "score_breakdown": score_breakdown,
            "buying_intent_score": int(intent_analysis.get("buying_intent_score", 0) or 0),
            "service_demand_score": int(intent_analysis.get("service_demand_score", 0) or 0),
            "urgency_score": int(intent_analysis.get("urgency_score", 0) or 0),
            "intent_summary": str(intent_analysis.get("intent_summary", "")).strip(),
            "intent_signals": list(intent_analysis.get("signals", []) or []),
            "quality_score": quality_filter["score"],
            "quality_reason": quality_filter["reason"],
            "quality_category": quality_filter["category"],
            "is_directory": bool(quality_filter.get("is_directory", False)),
            "domain_type": str(quality_filter.get("domain_type", "")).strip(),
            "email_status": scored["email_status"] if scored["email_status"] == "no_email" else lead_status,
            "scraping_method": str(input_json.get("scraping_method", "")).strip(),
            "scrape_status": str(input_json.get("scrape_status", "")).strip() or lead_status,
            "website_text_length": int(input_json.get("website_text_length", len(website_text)) or 0),
            "lead_status": lead_status,
            "ai_mode": ai_mode,
            "generic_email": bool(analysis.get("generic_email", legacy.is_generic_email_address(contact_info["email"]))),
            "email_channel_eligible": not bool(analysis.get("generic_email", legacy.is_generic_email_address(contact_info["email"]))),
            "phone_only_eligible": bool(analysis.get("phone_only_eligible", False)),
            "save_reason_note": "; ".join(dict.fromkeys(safety_notes)),
            "crawl_metrics": dict(input_json.get("crawl_metrics", {}) or {}),
        }
        if lead["phone_only_eligible"]:
            lead["readiness"] = "phone_ready"
        elif lead["generic_email"]:
            lead["readiness"] = "research_needed"
        else:
            lead["readiness"] = legacy.beta_lead_readiness(lead)
        demo_mode = legacy.leadgen_demo_mode_enabled()
        lead["scraped_email_invalid"] = scraped_email_invalid
        demo_excluded = self._demo_domain_excluded(lead, website_text)
        if demo_mode:
            lead["demo_mode"] = True
            lead["demo_accepted_contact"] = bool((scraped_email_present or scraped_phone_present) and not demo_excluded)
            lead["qualified"] = lead["demo_accepted_contact"]
            if lead["qualified"]:
                if scraped_phone_present and lead["contact_page"] and not scraped_email_present:
                    demo_reason = "Company has a public phone number and contact page."
                elif scraped_email_present and scraped_phone_present:
                    demo_reason = "Business website with public email/phone contact found."
                else:
                    demo_reason = "Website has scraped business contact details."
                lead["reason"] = demo_reason
                lead["save_reason_note"] = demo_reason
        else:
            lead["qualified"] = legacy.is_qualified_lead(lead)
        lead["decision"] = "accepted" if lead["qualified"] else "stored_partial"
        if not lead["qualified"]:
            scrape_status = str(lead.get("scrape_status", "") or "").strip().lower()
            if scrape_status not in {"", "ok", "pending"}:
                lead["skip_reason"] = "scrape_failed"
            elif demo_mode and demo_excluded:
                lead["skip_reason"] = "junk_source"
            elif demo_mode and not (scraped_email_present or scraped_phone_present):
                lead["skip_reason"] = "no_contact"
            elif not lead["relevance_passed"] and not demo_mode:
                lead["skip_reason"] = "relevance_not_passed"
            elif lead["readiness"] == "research_needed":
                lead["skip_reason"] = "no_contact"
            elif lead["is_directory"]:
                lead["skip_reason"] = "directory_or_listing"
            elif lead["domain_type"] in {"excluded", "non_business"}:
                lead["skip_reason"] = "junk_source"
            elif lead["domain_type"] == "listing":
                lead["skip_reason"] = "directory_or_listing"
            elif lead.get("scraped_email_invalid", False):
                lead["skip_reason"] = "invalid_email"
            else:
                lead["skip_reason"] = "no_contact"

        return {"lead": lead, "analysis": analysis, "quality_filter": quality_filter}

    @staticmethod
    def _demo_domain_excluded(lead: Dict[str, Any], website_text: str) -> bool:
        if bool(lead.get("is_directory", False)):
            return True
        if str(lead.get("domain_type", "") or "").strip().lower() in {"excluded", "listing", "non_business"}:
            return True
        combined = " ".join(
            [
                str(lead.get("website", "") or ""),
                str(lead.get("company_name", "") or ""),
                str(website_text or "")[:1500],
            ]
        ).lower()
        excluded_markers = (
            "facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com", "youtube.com",
            "job board", "jobs portal", "careers portal", "indeed.com", "rozee.pk", "glassdoor.com",
            "government", "ministry", "embassy", "consulate", ".gov", "gov.pk",
            "publisher", "magazine", "newspaper", "journal", "news portal", "forbes",
        )
        return any(marker in combined for marker in excluded_markers)

    @staticmethod
    def _build_provider_payload(input_json: Dict[str, Any], website_text_limit: int = 1200) -> Dict[str, Any]:
        email_present = bool(str(input_json.get("email", "") or "").strip())
        phone_present = bool(str(input_json.get("phone", "") or "").strip())
        contact_page = bool(str(input_json.get("contact_page", "") or "").strip())
        short_contact_summary = str(input_json.get("short_contact_summary", "") or "").strip()
        if not short_contact_summary:
            available = [
                label
                for label, present in (
                    ("email available", email_present),
                    ("phone available", phone_present),
                    ("contact page available", contact_page),
                )
                if present
            ]
            short_contact_summary = ", ".join(available) or "No direct contact details found."
        return {
            "website": str(input_json.get("website", "") or "").strip()[:300],
            "company_name": str(input_json.get("company_name", "") or "").strip()[:120],
            "query": str(input_json.get("query", "") or "").strip()[:160],
            "website_text": str(input_json.get("website_text", "") or "").strip()[:website_text_limit],
            "email_present": email_present,
            "phone_present": phone_present,
            "contact_page": contact_page,
            "short_contact_summary": short_contact_summary[:300],
        }

    @staticmethod
    def _provider_payload_was_truncated(input_json: Dict[str, Any]) -> bool:
        return any(
            len(str(input_json.get(field, "") or "").strip()) > limit
            for field, limit in (
                ("website", 300),
                ("company_name", 120),
                ("query", 160),
                ("website_text", 1200),
                ("short_contact_summary", 300),
            )
        )

    def _provider_analysis(self, provider_payload: Dict[str, Any], runtime: Dict[str, Any]) -> tuple[Dict[str, Any], str]:
        import leads as legacy

        tenant = runtime.get("tenant")
        if tenant is None:
            legacy.LOGGER.info("AI scoring skipped provider=unconfigured reason=tenant_provider_runtime_unavailable")
            return {}, "fallback"

        async def call_provider() -> tuple[Dict[str, Any], str]:
            from app.services.ai_provider_service import AIProviderService
            from app.db.session import get_async_db_session

            async with get_async_db_session() as db:
                service = AIProviderService(db)
                status = await service.status(tenant)
                provider = str(status.get("provider", "fallback") or "fallback")
                if not status.get("configured") or not status.get("enabled") or provider == "fallback":
                    raise RuntimeError(f"provider_not_configured:{provider}")
                current_payload = dict(provider_payload)
                for attempt in range(2):
                    payload_text = json.dumps(current_payload, ensure_ascii=False, separators=(",", ":"))
                    payload_truncated = bool(runtime.get("_ai_payload_truncated", False)) or attempt > 0
                    legacy.LOGGER.info(
                        "AI scoring payload provider=%s attempt=%s ai_payload_chars=%s ai_payload_truncated=%s",
                        provider,
                        attempt + 1,
                        len(payload_text),
                        payload_truncated,
                    )
                    system_prompt, user_prompt = legacy.build_lead_analysis_prompts(payload_text)
                    try:
                        result = await service.generate_text(
                            tenant=tenant,
                            system_prompt=system_prompt,
                            user_prompt=user_prompt,
                            json_mode=True,
                            temperature=0,
                            max_tokens=900,
                        )
                        return dict(result), provider
                    except Exception as error:
                        status_code = getattr(getattr(error, "response", None), "status_code", None)
                        status_code = status_code or getattr(error, "status_code", None)
                        if status_code == 413 and attempt == 0:
                            current_payload["website_text"] = str(current_payload.get("website_text", ""))[:500]
                            legacy.LOGGER.warning(
                                "AI scoring payload rejected provider=%s provider_status=413; retrying_with_smaller_payload=true",
                                provider,
                            )
                            continue
                        raise
                raise RuntimeError("provider_payload_retry_exhausted")

        provider = "unknown"
        try:
            target_loop = runtime.get("event_loop")
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None

            if target_loop is not None:
                if target_loop.is_closed() or not target_loop.is_running():
                    raise RuntimeError("provider_event_loop_unavailable")
                if running_loop is target_loop:
                    raise RuntimeError("provider_analysis_requires_async_await")
                provider_future = asyncio.run_coroutine_threadsafe(call_provider(), target_loop)
                analysis, provider = provider_future.result(timeout=45)
            elif running_loop is None:
                analysis, provider = asyncio.run(call_provider())
            else:
                raise RuntimeError("provider_analysis_requires_async_await")
            legacy.LOGGER.info("AI scoring completed provider=%s mode=provider", provider)
            return analysis, provider
        except Exception as error:
            safe_error = self._safe_provider_error(error)
            provider_match = re.search(r"provider_not_configured:([a-z]+)", safe_error)
            if provider_match:
                provider = provider_match.group(1)
            status_code = getattr(getattr(error, "response", None), "status_code", "")
            legacy.LOGGER.warning(
                "AI scoring failed provider=%s provider_status=%s reason=%s; fallback=python",
                provider,
                status_code or "unavailable",
                safe_error,
            )
            return {}, "fallback"

    @staticmethod
    def _safe_provider_error(error: Exception) -> str:
        text = f"{type(error).__name__}: {error}"
        text = re.sub(r"\b(?:sk|gsk|AIza)[A-Za-z0-9._-]{8,}\b", "[redacted]", text)
        text = re.sub(r"(?i)(api[_-]?key|authorization|token)(\s*[:=]\s*)\S+", r"\1\2[redacted]", text)
        return re.sub(r"\s+", " ", text).strip()[:300]

    @staticmethod
    def _validated_company_name(company_name: str, website: str) -> tuple[str, str]:
        import leads as legacy

        candidate = re.sub(r"\s+", " ", str(company_name or "")).strip()
        words = candidate.split()
        invalid_start = bool(words and words[0].lower().strip(".,:;!?") in {
            "i", "we", "our", "they", "read", "get", "want", "discover", "learn", "see", "find",
        })
        invalid = bool('"' in candidate or "'" in candidate or len(words) > 12 or invalid_start)
        if candidate and not invalid:
            return candidate, ""
        domain = legacy.root_domain_from_url(website).split(".")[0]
        fallback = re.sub(r"[-_]+", " ", domain).strip().title() or "Unknown Company"
        return fallback, "company name extraction fallback used — scraped text rejected as non-name"

    def _fallback_analysis(self, query: str, website_text: str, company_name: str, notes: list[str] | None = None) -> Dict[str, Any]:
        lowered_text = str(website_text or "").lower()
        industry = "Other"
        for candidate, keywords in self.INDUSTRY_KEYWORDS.items():
            if any(re.search(rf"\b{re.escape(keyword)}\b", lowered_text) for keyword in keywords):
                industry = candidate
                break
        subject = str(company_name or "").strip() or "This website"
        reason = f"{subject} requires manual relevance review because provider scoring was unavailable."
        if notes:
            reason = f"{reason} {'; '.join(notes)}"
        return {
            "company_summary": self._summary_from_text(website_text, company_name),
            "industry": industry,
            "needs_it_services": True,
            "relevance_passed": False,
            "extracted_email": "",
            "lead_score": 0,
            "reason": reason,
            "intent_analysis": {
                "buying_intent_score": 0,
                "service_demand_score": 0,
                "urgency_score": 0,
                "intent_summary": reason,
                "signals": [],
            },
        }

    def _industry_from_query(self, query: str) -> str:
        # TODO: full country/industry semantic relevance matching requires AI scoring (see Task A) —
        # this fallback only catches obvious mismatches.
        lowered = str(query or "").lower()
        for industry, keywords in self.INDUSTRY_KEYWORDS.items():
            if any(re.search(rf"\b{re.escape(keyword)}\b", lowered) for keyword in keywords):
                return industry
        return "Other"

    def _service_reason(self, industry: str, query: str, company_name: str) -> str:
        subject = str(company_name or "").strip() or "This company"
        if industry and industry != "Other":
            return f"{subject} appears to offer {industry.lower()} services and may benefit from a steadier flow of qualified prospects."
        return f"{subject} appears to be a relevant business with public contact details."

    def _summary_from_text(self, website_text: str, company_name: str) -> str:
        text = re.sub(r"\s+", " ", str(website_text or "")).strip()
        if text:
            return text[:240]
        return str(company_name or "").strip()


class EmailAgent(JsonAgent):
    name = "EmailAgent"

    def _run(self, input_json: Dict[str, Any]) -> Dict[str, Any]:
        import leads as legacy

        business_info = dict(input_json.get("business_info", {}) or {})
        website_summary = str(input_json.get("website_summary", "")).strip()
        lead_score = input_json.get("lead_score", 0)
        email_package = legacy.generate_email_variants(
            business_info=business_info,
            website_summary=website_summary,
            lead_score=lead_score,
        )
        return {
            "business_info": business_info,
            "website_summary": website_summary,
            "lead_score": lead_score,
            "personalized_hook": email_package["personalized_hook"],
            "cta": email_package["cta"],
            "variants": email_package["variants"],
        }


class OutreachAgent(JsonAgent):
    name = "OutreachAgent"

    def _run(self, input_json: Dict[str, Any]) -> Dict[str, Any]:
        import leads as legacy

        lead = dict(input_json.get("lead", {}) or {})
        company = str(lead.get("Company", lead.get("company_name", ""))).strip() or "Unknown Company"
        email = str(lead.get("Email", lead.get("email", ""))).strip()
        website = str(lead.get("Website", lead.get("website", ""))).strip()
        if not email:
            raise ValueError("OutreachAgent requires lead.Email or lead.email.")

        email_lead = {
            **lead,
            "Company": company,
            "Website": website,
            "Email": email,
            "Reason": str(lead.get("Reason", lead.get("reason", ""))).strip(),
        }
        email_variants = EmailAgent().run(
            {
                "business_info": {
                    "company_name": company,
                    "website": website,
                    "industry": str(lead.get("Industry", lead.get("industry", ""))).strip(),
                },
                "website_summary": str(lead.get("company_summary", lead.get("CompanySummary", ""))).strip()
                or str(lead.get("Reason", lead.get("reason", ""))).strip(),
                "lead_score": lead.get("lead_score", lead.get("Score", 0)),
            }
        )
        primary_variant = email_variants["variants"][0]
        subject = primary_variant["subject"]
        body = primary_variant["body"]
        result = legacy.send_email_gmail(email, subject, body)
        next_followup_due = legacy.to_iso8601(legacy.now_utc() + timedelta(days=2))

        return {
            "company": company,
            "email": email,
            "website": website,
            "status": "Sent",
            "subject": subject,
            "personalized_hook": email_variants["personalized_hook"],
            "cta": email_variants["cta"],
            "variants": email_variants["variants"],
            "gmail_thread_id": str(result.get("threadId", "")),
            "next_followup_due": next_followup_due,
        }


def run_agent(agent: JsonAgent, input_json: Dict[str, Any]) -> Dict[str, Any]:
    return agent.run(input_json)
