"""Rule-based AI Marketing Campaign Kit generation."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from typing import Any, Dict, Sequence
from urllib.parse import urlparse

from app.core.models import Lead, Tenant, TenantContext
from app.db.session import AsyncDatabaseSession, DatabaseSession
from app.services.ai_provider_service import AIProviderNotConfigured, AIProviderService
from app.services._async import maybe_await
from app.services.skill_prompt_service import SkillPromptService


MARKETING_KIT_LIMITS = {
    "free": 3,
    "starter": 3,
    "pro": 100,
    "agency": 1000,
}


class MarketingCampaignLimitError(ValueError):
    """Raised when a tenant reaches the monthly marketing campaign limit."""


class MarketingCampaignService:
    def __init__(self, db: DatabaseSession | AsyncDatabaseSession) -> None:
        self.db = db

    async def generate_from_idea(
        self,
        tenant: TenantContext,
        business_idea: str,
        target_location: str = "",
        target_audience: str = "",
        campaign_goal: str = "",
    ) -> Dict[str, Any]:
        await self._consume_usage(tenant)
        campaign = self.build_campaign(
            business_idea=business_idea,
            target_location=target_location,
            target_audience=target_audience,
            campaign_goal=campaign_goal,
        )
        original_input = {
            "source": "business_idea",
            "business_idea": business_idea,
            "target_location": target_location,
            "target_audience": target_audience,
            "campaign_goal": campaign_goal,
        }
        return await self._enhance_with_ai(tenant, campaign, "campaign_generator", original_input)

    async def generate_from_lead(self, tenant: TenantContext, lead_id: str) -> Dict[str, Any]:
        lead = await maybe_await(self.db.leads.get(tenant.tenant_id, lead_id))
        if lead is None:
            raise ValueError("Lead not found.")
        await self._consume_usage(tenant)
        campaign = self.build_from_lead(lead)
        campaign = await self._enhance_with_ai(
            tenant,
            campaign,
            "campaign_generator",
            {"source": "lead", "lead": self._lead_context(lead)},
        )
        metadata = dict(lead.metadata or {})
        metadata["marketing_campaign_kit"] = campaign
        lead.metadata = metadata
        await maybe_await(self.db.for_tenant(tenant).save("leads", lead))
        return campaign

    def build_from_lead(self, lead: Lead) -> Dict[str, Any]:
        metadata = dict(lead.metadata or {})
        agency_kit = metadata.get("agency_kit", {}) if isinstance(metadata.get("agency_kit", {}), dict) else {}
        service = str(agency_kit.get("recommended_service") or lead.service_reason or lead.industry or "lead capture system")
        company = self._company_label(lead)
        location = str(lead.country or lead.location or "").strip()
        audience = f"decision makers interested in {service.lower()}"
        goal = "Generate qualified inquiries from this lead or similar businesses"
        idea = f"{service} for {company}"
        if location:
            idea = f"{idea} in {location}"
        return self.build_campaign(
            business_idea=idea,
            target_location=location,
            target_audience=audience,
            campaign_goal=goal,
            lead=lead,
        )

    def build_campaign(
        self,
        business_idea: str,
        target_location: str = "",
        target_audience: str = "",
        campaign_goal: str = "",
        lead: Lead | None = None,
    ) -> Dict[str, Any]:
        normalized_idea = str(business_idea or "").strip() or "service business growth campaign"
        location = str(target_location or "").strip()
        audience = str(target_audience or "").strip() or self._default_audience(normalized_idea)
        goal = str(campaign_goal or "").strip() or f"Promote {normalized_idea} and drive customer inquiries"
        profile = self._category_profile(normalized_idea, lead)
        if profile.get("category") == "study_abroad":
            return self._study_abroad_campaign(normalized_idea, location, audience, goal)
        if profile.get("category") in {"immigration_consultancy", "visa_consultancy"}:
            return self._visa_consultancy_campaign(normalized_idea, location, audience, goal, profile)
        offer = profile["offer"]
        pain = profile["pain_points"][0]
        location_phrase = f" in {location}" if location else ""
        return {
            "mode": "fallback",
            "campaign_goal": goal,
            "business_idea": normalized_idea,
            "target_audience": audience,
            "recommended_platforms": ["Facebook/Instagram", "Google Search", "Reels/TikTok"],
            "budget_plan": self._budget_plan(),
            "facebook_instagram_ads": self._facebook_ads(offer, audience, pain, location_phrase),
            "google_search_ads": self._google_ads(offer, pain, location_phrase),
            "reels_tiktok_script": self._reels_script(offer, pain),
            "landing_page_copy": self._landing_page_copy(offer, profile["pain_points"]),
            "lead_magnet": profile["lead_magnet"],
            "seven_day_content_calendar": self._content_calendar(offer, audience),
            "next_action": "Launch the campaign with one offer, one landing page, and a small test budget for 7 days.",
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    async def _enhance_with_ai(
        self,
        tenant: TenantContext,
        payload: Dict[str, Any],
        skill_name: str,
        original_input: Dict[str, Any],
    ) -> Dict[str, Any]:
        service = AIProviderService(self.db)
        try:
            system_prompt = SkillPromptService().load_skill(skill_name)
            user_prompt = json.dumps(
                {
                    "original_input": original_input,
                    "rule_based_output": payload,
                    "instruction": (
                        "Improve the rule-based output, but preserve the existing JSON schema, required keys, "
                        "types, tenant context, and safety constraints. Always market the user's original business "
                        "idea and end customer. Do not switch to Lead Hunter AI, agency services, lead generation, "
                        "websites, funnels, or landing pages unless the user explicitly asked for that service. "
                        "Return valid JSON only."
                    ),
                },
                ensure_ascii=True,
                default=str,
            )
            enhanced = await service.generate_text(
                tenant,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                json_mode=True,
                temperature=0.25,
                max_tokens=1800,
            )
            status = await service.status(tenant)
        except AIProviderNotConfigured:
            return payload
        except Exception:
            fallback = dict(payload)
            fallback["mode"] = "fallback"
            fallback["ai_error"] = "AI provider failed; fallback used"
            return fallback
        if isinstance(enhanced, dict) and self._has_campaign_shape(enhanced):
            merged = self._merge_ai_output(payload, enhanced)
            merged["mode"] = "ai_enhanced"
            merged["provider"] = status.get("provider", "")
            merged["model"] = status.get("model", "")
            return merged
        merged = dict(payload)
        merged["mode"] = "ai_enhanced"
        merged["provider"] = status.get("provider", "")
        merged["model"] = status.get("model", "")
        merged["ai_notes"] = str(enhanced)
        return merged

    def _has_campaign_shape(self, enhanced: Dict[str, Any]) -> bool:
        expected_keys = {
            "campaign_goal",
            "business_idea",
            "target_audience",
            "facebook_instagram_ads",
            "google_search_ads",
            "reels_tiktok_script",
            "landing_page_copy",
            "seven_day_content_calendar",
        }
        return any(key in enhanced for key in expected_keys)

    def _merge_ai_output(self, payload: Dict[str, Any], enhanced: Dict[str, Any]) -> Dict[str, Any]:
        merged = dict(payload)
        for key in payload:
            if key in enhanced:
                merged[key] = enhanced[key]
        return merged

    def _lead_context(self, lead: Lead) -> Dict[str, Any]:
        metadata = dict(lead.metadata or {})
        agency_kit = metadata.get("agency_kit", {}) if isinstance(metadata.get("agency_kit"), dict) else {}
        return {
            "company_url": str(lead.company_url or lead.website or "").strip(),
            "company": self._company_label(lead),
            "industry": str(lead.industry or "").strip(),
            "country": str(lead.country or lead.location or "").strip(),
            "score": int(lead.score or lead.lead_score or 0),
            "service_reason": str(lead.service_reason or lead.reason or "").strip(),
            "agency_kit": agency_kit,
        }

    async def _consume_usage(self, tenant: TenantContext) -> None:
        tenant_record = await self._get_tenant(tenant.tenant_id)
        plan = str(tenant_record.subscription_plan or "Free").strip().lower()
        limit = MARKETING_KIT_LIMITS.get(plan, MARKETING_KIT_LIMITS["free"])
        period = datetime.now(timezone.utc).strftime("%Y-%m")
        settings = dict(tenant_record.settings or {})
        usage = dict(settings.get("marketing_campaign_usage", {}))
        used = int(usage.get(period, 0) or 0)
        if used >= limit:
            raise MarketingCampaignLimitError(f"Marketing Kit monthly limit reached: {used}/{limit}.")
        usage[period] = used + 1
        settings["marketing_campaign_usage"] = usage
        tenant_record.settings = settings
        await maybe_await(self.db.tenants.save(tenant_record))

    async def _get_tenant(self, tenant_id: str) -> Tenant:
        tenants = await maybe_await(self.db.tenants.list(tenant_id))
        if not tenants:
            raise ValueError("Tenant does not exist.")
        return tenants[0]

    def _category_profile(self, business_idea: str, lead: Lead | None = None) -> Dict[str, Any]:
        lead_text = ""
        if lead is not None:
            metadata = lead.metadata or {}
            lead_text = " ".join(
                [
                    str(lead.industry or ""),
                    str(lead.service_reason or ""),
                    str(lead.reason or ""),
                    str(metadata.get("industry", "")),
                ]
            )
        text = f"{business_idea} {lead_text}".lower()
        profiles = [
            ({"study permit", "canada study", "student visa", "study visa", "study abroad", "admission", "college", "university", "ielts"}, "study_abroad", "Canada study permit consultation and admission guidance", ["confusing study permit documents", "college or program selection uncertainty", "visa refusal worries"], "Free Canada study permit document checklist"),
            ({"web design", "website", "landing page"}, "high-converting website and landing page package", ["unclear online offer", "weak conversion path", "outdated web presence"], "Free homepage conversion audit"),
            ({"lead generation", "leads", "sales funnel"}, "lead generation and follow-up funnel", ["weak lead pipeline", "manual outreach", "no follow-up system"], "Free lead funnel scorecard"),
            ({"immigration", "consultancy"}, "immigration_consultancy", "immigration consultation and application guidance", ["confusing eligibility requirements", "document preparation worries", "unclear application steps"], "Free visa eligibility checklist"),
            ({"visa", "work permit", "visit visa", "family sponsorship"}, "visa_consultancy", "visa consultation and document review service", ["document confusion", "application refusal worries", "unclear process timelines"], "Free visa document checklist"),
            ({"restaurant", "food", "cafe", "catering"}, "local food promotion and WhatsApp ordering campaign", ["weak local visibility", "low repeat orders", "missing review strategy"], "Limited-time menu offer"),
            ({"real estate", "property", "broker"}, "property lead capture and WhatsApp nurture campaign", ["leads lost from social media", "slow follow-up", "weak landing pages"], "Free property buyer checklist"),
            ({"clinic", "healthcare", "doctor", "dental", "medical"}, "appointment booking and patient lead campaign", ["missed appointment inquiries", "weak Google reviews", "unclear service pages"], "Free consultation request form"),
            ({"education", "training", "school", "academy", "course"}, "admissions lead funnel and nurture campaign", ["student inquiry leakage", "manual follow-up", "weak conversion tracking"], "Free admissions guide"),
            ({"ecommerce", "retail", "shop", "store", "product"}, "product offer campaign with abandoned inquiry follow-up", ["weak product offer clarity", "low conversion", "no retargeting flow"], "First-order discount or buying guide"),
            ({"local service", "cleaning", "repair", "construction", "contractor"}, "local service lead campaign and quote request funnel", ["weak online trust", "no quote request funnel", "missing service pages"], "Free quote checklist"),
            ({"software", "it", "technology", "saas", "cloud", "cyber"}, "B2B demo and consultation campaign", ["long sales cycles", "unclear technical value", "low demo volume"], "Free automation audit"),
        ]
        for profile in profiles:
            if len(profile) == 5:
                keywords, category, offer, pain_points, lead_magnet = profile
            else:
                keywords, offer, pain_points, lead_magnet = profile
                category = "generic"
            if any(self._matches_keyword(text, keyword) for keyword in keywords):
                return {"category": category, "offer": offer, "pain_points": pain_points, "lead_magnet": lead_magnet}
        return {
            "category": "default",
            "offer": f"{business_idea} offer",
            "pain_points": ["unclear options", "hesitation before taking action", "not knowing what to choose first"],
            "lead_magnet": "Free buyer guide or checklist",
        }

    def _matches_keyword(self, text: str, keyword: str) -> bool:
        normalized = str(keyword or "").strip().lower()
        if not normalized:
            return False
        if len(normalized) <= 3:
            return bool(re.search(rf"\b{re.escape(normalized)}\b", text))
        return normalized in text

    def _default_audience(self, business_idea: str) -> str:
        return f"people actively looking for {business_idea.lower()}"

    def _budget_plan(self) -> Dict[str, str]:
        return {
            "starter": "$5-$15/day for 7 days to validate one offer and one audience.",
            "growth": "$20-$50/day with separate ad sets for search intent and retargeting.",
            "agency": "$75+/day with creative testing, conversion tracking, and weekly reporting.",
        }

    def _facebook_ads(self, offer: str, audience: str, pain: str, location_phrase: str) -> list[Dict[str, str]]:
        return [
            {
                "primary_text": f"Still dealing with {pain}? Explore a practical {offer}{location_phrase} designed to make the next step clear.",
                "headline": "Get clear guidance today",
                "description": f"A practical {offer} for {audience}.",
                "cta": "Book a Free Call",
            },
            {
                "primary_text": f"Not sure where to start? Get simple guidance, understand your options, and choose the path that fits your situation.",
                "headline": "Know your next step",
                "description": f"Helpful guidance for {audience}.",
                "cta": "Request Guidance",
            },
        ]

    def _google_ads(self, offer: str, pain: str, location_phrase: str) -> list[Dict[str, str]]:
        return [
            {
                "headline_1": "Find The Right Guidance",
                "headline_2": "Book A Free Consultation",
                "headline_3": "Clear Next Steps",
                "description_1": f"Fix {pain} with a focused {offer}{location_phrase}.",
                "description_2": "Understand your options, ask questions, and choose the best next step.",
            }
        ]

    def _reels_script(self, offer: str, pain: str) -> Dict[str, str]:
        return {
            "hook": f"Feeling stuck because of {pain}?",
            "script": f"Show the common confusion people face, then explain how a {offer} helps them understand options, avoid mistakes, and take the next step with confidence.",
            "cta": "Comment 'guide' or book a free consultation.",
        }

    def _landing_page_copy(self, offer: str, pain_points: Sequence[str]) -> Dict[str, Any]:
        return {
            "headline": f"Get clear guidance for {offer}",
            "subheadline": "Understand your options, avoid common mistakes, and choose your next step with confidence.",
            "bullets": [
                f"Reduce {pain_points[0]}",
                "Get a simple explanation of your best options",
                "Book a consultation before you make a costly decision",
            ],
            "cta": "Book a free consultation",
        }

    def _content_calendar(self, offer: str, audience: str) -> list[Dict[str, Any]]:
        ideas = [
            ("Problem awareness", f"Explain the most common confusion {audience} face before choosing {offer}.", "Book a short call"),
            ("Before and after", f"Show how the right {offer} changes the decision process.", "Explore your options"),
            ("Trust builder", "Share proof points, testimonials, or a simple service process.", "Ask a question"),
            ("Offer clarity", f"Break down what is included in the {offer}.", "Ask for pricing"),
            ("FAQ post", f"Answer the top objections from {audience}.", "Send your question"),
            ("Quick tip", "Share one simple improvement people can make today.", "Save this idea"),
            ("Soft pitch", "Invite people to book a free consultation with no pressure.", "Book a consultation"),
        ]
        return [
            {
                "day": index,
                "post_idea": title,
                "caption": caption,
                "cta": cta,
            }
            for index, (title, caption, cta) in enumerate(ideas, start=1)
        ]

    def _study_abroad_campaign(self, business_idea: str, location: str, audience: str, goal: str) -> Dict[str, Any]:
        destination = self._study_destination(business_idea)
        source_country = self._source_country(location)
        student_label = self._source_adjective(source_country)
        audience_label = self._study_audience(audience, student_label, destination)
        location_phrase = f" from {source_country}" if source_country else ""
        campaign_goal = self._study_campaign_goal(goal, destination)
        return {
            "mode": "fallback",
            "campaign_goal": campaign_goal,
            "business_idea": business_idea,
            "target_audience": audience_label,
            "recommended_platforms": ["Facebook/Instagram", "Google Search", "Reels/TikTok"],
            "budget_plan": self._study_budget_plan(source_country),
            "facebook_instagram_ads": self._study_facebook_ads(destination, source_country, audience_label),
            "google_search_ads": self._study_google_ads(destination, source_country),
            "reels_tiktok_script": self._study_reels_script(destination),
            "landing_page_copy": self._study_landing_page_copy(destination, source_country),
            "lead_magnet": f"{destination} study permit document checklist{location_phrase}",
            "seven_day_content_calendar": self._study_content_calendar(destination, source_country),
            "next_action": f"Launch one consultation booking page for {destination} study permit guidance and test student-focused ads for 7 days.",
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    def _visa_consultancy_campaign(
        self,
        business_idea: str,
        location: str,
        audience: str,
        goal: str,
        profile: Dict[str, Any],
    ) -> Dict[str, Any]:
        location_phrase = f" in {location}" if location else ""
        audience_label = audience or self._default_audience(business_idea)
        offer = str(profile.get("offer") or "visa consultation and document review service")
        pain_points = list(profile.get("pain_points") or ["document confusion", "application refusal worries", "unclear process timelines"])
        return {
            "mode": "fallback",
            "campaign_goal": goal,
            "business_idea": business_idea,
            "target_audience": audience_label,
            "recommended_platforms": ["Facebook/Instagram", "Google Search", "Reels/TikTok"],
            "budget_plan": self._budget_plan(),
            "facebook_instagram_ads": [
                {
                    "primary_text": f"Planning a visa application{location_phrase}? Get clear guidance on eligibility, documents, and next steps before you apply.",
                    "headline": "Visa Consultation",
                    "description": f"Practical guidance for {audience_label}.",
                    "cta": "Book Consultation",
                }
            ],
            "google_search_ads": [
                {
                    "headline_1": "Visa Consultant",
                    "headline_2": "Document Review Help",
                    "headline_3": "Book Consultation",
                    "description_1": f"Get help with eligibility, documents, and application steps{location_phrase}.",
                    "description_2": "Book a consultation before you submit your visa application.",
                }
            ],
            "reels_tiktok_script": {
                "hook": "Visa application confused? Start with these three checks.",
                "script": f"Explain eligibility, document quality, and timeline planning. Then show how a {offer} helps applicants avoid simple mistakes before submission.",
                "cta": "Book a visa consultation.",
            },
            "landing_page_copy": {
                "headline": "Book a visa eligibility consultation",
                "subheadline": "Get clear guidance on documents, application steps, and common refusal risks before you apply.",
                "bullets": [
                    f"Reduce {pain_points[0]}",
                    "Review eligibility and document readiness",
                    "Understand the next step before submitting",
                ],
                "cta": "Book a consultation",
            },
            "lead_magnet": str(profile.get("lead_magnet") or "Free visa document checklist"),
            "seven_day_content_calendar": self._visa_content_calendar(),
            "next_action": "Launch one consultation offer with eligibility review, document checklist, and call booking.",
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    def _study_budget_plan(self, source_country: str) -> Dict[str, str]:
        location_note = f" in {source_country}" if source_country else ""
        return {
            "starter": f"$5-$15/day for 7 days targeting students and parents{location_note} interested in studying abroad.",
            "growth": "$20-$50/day split between search-intent ads, student/parent creatives, and retargeting.",
            "agency": "$75+/day with city-level testing, consultation booking tracking, and weekly creative refresh.",
        }

    def _study_facebook_ads(self, destination: str, source_country: str, audience: str) -> list[Dict[str, str]]:
        source_phrase = f" from {source_country}" if source_country else ""
        return [
            {
                "primary_text": f"Want to study in {destination}{source_phrase} but confused about documents, college selection, or study permit steps? Book a free eligibility check and get a clear path before you apply.",
                "headline": f"Study In {destination}",
                "description": f"Guidance for {audience}.",
                "cta": "Book Free Eligibility Check",
            },
            {
                "primary_text": f"Worried about choosing the wrong program or missing an important visa document? Get {destination} study permit guidance for admissions, documents, and consultation booking.",
                "headline": "Student Visa Guidance",
                "description": "Admissions, document checklist, and study permit consultation.",
                "cta": "Book Consultation",
            },
        ]

    def _study_google_ads(self, destination: str, source_country: str) -> list[Dict[str, str]]:
        source_phrase = f" From {source_country}" if source_country else ""
        return [
            {
                "headline_1": f"{destination} Study Visa Consultant",
                "headline_2": f"Study In {destination}{source_phrase}",
                "headline_3": "Student Visa Help",
                "description_1": f"Get {destination} study permit guidance, admission support, and document checklist help.",
                "description_2": "Book a consultation before choosing a college or submitting your visa file.",
            }
        ]

    def _study_reels_script(self, destination: str) -> Dict[str, str]:
        return {
            "hook": f"Planning to study in {destination}? Do not submit your file before checking these documents.",
            "script": "Show a student confused about bank statements, SOP, admission letter, program choice, and refusal worries. Then explain that the right eligibility review can reduce simple mistakes and make the study permit process clearer.",
            "cta": "Book a free eligibility check for your study permit plan.",
        }

    def _study_landing_page_copy(self, destination: str, source_country: str) -> Dict[str, Any]:
        source_phrase = f" for students from {source_country}" if source_country else ""
        return {
            "headline": f"Free {destination} Study Permit Eligibility Check",
            "subheadline": f"Get {destination} study permit guidance{source_phrase}, including admissions, document checklist, and consultation booking.",
            "bullets": [
                "Review your eligibility before you apply",
                "Get a clear document checklist for your study permit file",
                "Discuss college, program, admission, and refusal-risk concerns",
            ],
            "cta": "Book a consultation",
        }

    def _study_content_calendar(self, destination: str, source_country: str) -> list[Dict[str, Any]]:
        source_phrase = f" from {source_country}" if source_country else ""
        ideas = [
            ("Eligibility checklist", f"What students{source_phrase} should check before applying for a {destination} study permit.", "Book eligibility check"),
            ("Document mistakes", "Common document gaps that can create stress during a student visa application.", "Get the checklist"),
            ("Program selection", f"How to think about college and program choice before planning to study in {destination}.", "Ask for guidance"),
            ("Parent-focused FAQ", "Questions parents usually ask about fees, timelines, and document readiness.", "Book a family consultation"),
            ("IELTS and admission", f"Where IELTS, admission letters, and SOP fit into the {destination} study visa process.", "Send your question"),
            ("Refusal worries", "What to review if you are worried about refusal risk before submitting your file.", "Book a file review"),
            ("Consultation invite", f"Invite students to a free {destination} study permit eligibility check.", "Book a consultation"),
        ]
        return [
            {"day": index, "post_idea": title, "caption": caption, "cta": cta}
            for index, (title, caption, cta) in enumerate(ideas, start=1)
        ]

    def _visa_content_calendar(self) -> list[Dict[str, Any]]:
        ideas = [
            ("Eligibility basics", "Explain what applicants should check before starting a visa application.", "Book eligibility review"),
            ("Document checklist", "Share the most commonly missed document categories.", "Get the checklist"),
            ("Timeline planning", "Explain why applicants should plan before deadlines get close.", "Ask about timelines"),
            ("Refusal risk", "Discuss common reasons applications become weak without making guarantees.", "Book file review"),
            ("FAQ", "Answer a common question about consultation, documents, or eligibility.", "Send your question"),
            ("Process breakdown", "Explain the application process in simple steps.", "Save this guide"),
            ("Consultation invite", "Invite applicants to book a call before submitting their file.", "Book consultation"),
        ]
        return [
            {"day": index, "post_idea": title, "caption": caption, "cta": cta}
            for index, (title, caption, cta) in enumerate(ideas, start=1)
        ]

    def _study_destination(self, business_idea: str) -> str:
        text = business_idea.lower()
        destinations = {
            "canada": "Canada",
            "australia": "Australia",
            "uk": "UK",
            "united kingdom": "UK",
            "usa": "USA",
            "united states": "USA",
            "germany": "Germany",
            "ireland": "Ireland",
        }
        for keyword, label in destinations.items():
            if self._matches_keyword(text, keyword):
                return label
        return "Canada" if "study permit" in text else "your destination country"

    def _source_country(self, location: str) -> str:
        cleaned = str(location or "").strip()
        if not cleaned:
            return ""
        lowered = cleaned.lower()
        countries = {
            "pakistan": "Pakistan",
            "india": "India",
            "bangladesh": "Bangladesh",
            "uae": "UAE",
            "dubai": "UAE",
            "saudi": "Saudi Arabia",
        }
        for keyword, label in countries.items():
            if keyword in lowered:
                return label
        return cleaned

    def _source_adjective(self, source_country: str) -> str:
        adjectives = {
            "Pakistan": "Pakistani",
            "India": "Indian",
            "Bangladesh": "Bangladeshi",
            "UAE": "UAE-based",
            "Saudi Arabia": "Saudi-based",
        }
        return adjectives.get(source_country, source_country)

    def _study_audience(self, audience: str, source_adjective: str, destination: str) -> str:
        normalized = str(audience or "").strip().lower()
        prefix = f"{source_adjective} " if source_adjective else ""
        if normalized in {"", "student", "students"}:
            return f"{prefix}students and parents planning to study in {destination}".strip()
        if "student" in normalized and source_adjective:
            return f"{source_adjective} {audience} and parents planning to study in {destination}"
        return audience

    def _study_campaign_goal(self, goal: str, destination: str) -> str:
        normalized = str(goal or "").strip()
        if not normalized:
            return f"Book consultation calls for {destination} study permit guidance"
        lowered = normalized.lower()
        if any(keyword in lowered for keyword in ["study", "visa", "permit", "consult"]):
            return normalized
        return f"{normalized} for {destination} study permit consultations"

    def _company_label(self, lead: Lead) -> str:
        for value in (lead.company_name, lead.company, lead.company_url, lead.website):
            normalized = str(value or "").strip()
            if normalized:
                if normalized.startswith(("http://", "https://")):
                    parsed = urlparse(normalized)
                    return parsed.netloc.replace("www.", "") or normalized
                return normalized
        return "this business"
