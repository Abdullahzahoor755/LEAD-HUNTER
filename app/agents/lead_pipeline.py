"""JSON-in/JSON-out agents for the legacy lead generation pipeline."""

from __future__ import annotations

import json
import os
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
        if not query:
            raise ValueError("DiscoveryAgent requires a non-empty 'query'.")
        limit = input_json.get("limit")
        limit_value = int(limit) if limit is not None else None
        seen_websites = {str(item) for item in input_json.get("seen_websites", []) if str(item).strip()}

        search_results = legacy.search_google(query)
        websites = legacy.extract_websites(search_results)
        candidate_websites = []
        for website in websites:
            website_key = legacy.get_website_key(website)
            if website_key in seen_websites:
                legacy.LOGGER.info("Skipping duplicate website already processed: %s", website)
                continue
            seen_websites.add(website_key)
            candidate_websites.append(website)
            if limit_value is not None and len(candidate_websites) >= limit_value:
                break

        return {
            "query": query,
            "search_results_count": len(search_results),
            "websites": candidate_websites,
            "website_count": len(candidate_websites),
            "seen_websites": sorted(seen_websites),
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
            "scrape_status": scrape_context.last_status or lead_status,
            "lead_status": lead_status,
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
        query = str(input_json.get("query", "")).strip()
        forced_ai_mode = str(input_json.get("ai_mode", "")).strip().lower()
        claude_available = bool(os.getenv("ANTHROPIC_API_KEY", "").strip())
        analysis: Dict[str, Any] = {}
        ai_mode = "claude"
        if website_text and forced_ai_mode != "fallback" and claude_available:
            analysis = legacy.analyze_lead_with_claude(website_text)
        if forced_ai_mode == "fallback" or not analysis:
            ai_mode = "fallback"
            analysis = self._fallback_analysis(query=query, website_text=website_text, company_name=contact_info["company_name"])
        analysis["ai_mode"] = ai_mode
        extracted_email = str(analysis.get("extracted_email", "")).strip().lower()
        if not contact_info["email"] and extracted_email and legacy.is_valid_email(extracted_email):
            contact_info["email"] = extracted_email
        if extracted_email and legacy.is_valid_email(extracted_email) and extracted_email not in contact_info["candidate_emails"]:
            contact_info["candidate_emails"].append(extracted_email)
            contact_info["email_candidates"].append(
                {"email": extracted_email, "source": "ai_extracted", "page_url": website, "confidence": "verified_email"}
            )
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

        reason = scored["reason"]
        score_breakdown = reason
        fallback_reason = str(input_json.get("fallback_reason", "")).strip()
        if fallback_reason:
            reason = f"{reason} | fallback_reason={fallback_reason}"
        reason = f"{reason} | quality_filter={quality_filter['reason']}"

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
            "email_status": scored["email_status"] if scored["email_status"] == "no_email" else lead_status,
            "scraping_method": str(input_json.get("scraping_method", "")).strip(),
            "scrape_status": str(input_json.get("scrape_status", "")).strip() or lead_status,
            "ai_mode": ai_mode,
        }
        lead["qualified"] = legacy.is_qualified_lead(lead)
        lead["decision"] = "accepted" if lead["qualified"] else "stored_partial"
        if not lead["qualified"]:
            lead["skip_reason"] = f"stored with low fit score ({lead['lead_score']})"

        return {"lead": lead, "analysis": analysis, "quality_filter": quality_filter}

    def _fallback_analysis(self, query: str, website_text: str, company_name: str) -> Dict[str, Any]:
        industry = self._industry_from_query(query)
        reason = self._service_reason(industry=industry, query=query, company_name=company_name)
        signals = [signal for signal in [industry, query] if signal]
        return {
            "company_summary": self._summary_from_text(website_text, company_name),
            "industry": industry,
            "needs_it_services": True,
            "extracted_email": "",
            "lead_score": 0,
            "reason": reason,
            "intent_analysis": {
                "buying_intent_score": 55 if industry != "Other" else 35,
                "service_demand_score": 60 if industry != "Other" else 40,
                "urgency_score": 30,
                "intent_summary": reason,
                "signals": signals,
            },
        }

    def _industry_from_query(self, query: str) -> str:
        lowered = str(query or "").lower()
        for industry, keywords in self.INDUSTRY_KEYWORDS.items():
            if any(re.search(rf"\b{re.escape(keyword)}\b", lowered) for keyword in keywords):
                return industry
        return "Other"

    def _service_reason(self, industry: str, query: str, company_name: str) -> str:
        subject = str(company_name or "").strip() or "This company"
        if industry and industry != "Other":
            return f"{subject} matches the {industry.lower()} target from the search query and may need workflow automation or managed IT support."
        if str(query or "").strip():
            return f"{subject} matches the search query and may need workflow automation or managed IT support."
        return f"{subject} may need workflow automation or managed IT support."

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
