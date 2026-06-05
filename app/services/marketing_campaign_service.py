"""Rule-based AI Marketing Campaign Kit generation."""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any, Dict, Sequence
from urllib.parse import urlparse

from app.core.models import Lead, Tenant, TenantContext
from app.db.session import AsyncDatabaseSession, DatabaseSession
from app.services._async import maybe_await


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
        return self.build_campaign(
            business_idea=business_idea,
            target_location=target_location,
            target_audience=target_audience,
            campaign_goal=campaign_goal,
        )

    async def generate_from_lead(self, tenant: TenantContext, lead_id: str) -> Dict[str, Any]:
        lead = await maybe_await(self.db.leads.get(tenant.tenant_id, lead_id))
        if lead is None:
            raise ValueError("Lead not found.")
        await self._consume_usage(tenant)
        campaign = self.build_from_lead(lead)
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
        goal = str(campaign_goal or "").strip() or "Generate qualified leads and inquiries"
        profile = self._category_profile(normalized_idea, lead)
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
            ({"web design", "website", "landing page"}, "high-converting website and landing page package", ["unclear online offer", "weak conversion path", "outdated web presence"], "Free homepage conversion audit"),
            ({"lead generation", "leads", "sales funnel"}, "lead generation and follow-up funnel", ["weak lead pipeline", "manual outreach", "no follow-up system"], "Free lead funnel scorecard"),
            ({"immigration", "visa", "consultancy"}, "immigration inquiry funnel and consultation booking flow", ["missed inquiries", "low trust signals", "manual consultation follow-up"], "Free visa eligibility checklist"),
            ({"restaurant", "food", "cafe", "catering"}, "local food promotion and WhatsApp ordering campaign", ["weak local visibility", "low repeat orders", "missing review strategy"], "Limited-time menu offer"),
            ({"real estate", "property", "broker"}, "property lead capture and WhatsApp nurture campaign", ["leads lost from social media", "slow follow-up", "weak landing pages"], "Free property buyer checklist"),
            ({"clinic", "healthcare", "doctor", "dental", "medical"}, "appointment booking and patient lead campaign", ["missed appointment inquiries", "weak Google reviews", "unclear service pages"], "Free consultation request form"),
            ({"education", "training", "school", "academy", "course"}, "admissions lead funnel and nurture campaign", ["student inquiry leakage", "manual follow-up", "weak conversion tracking"], "Free admissions guide"),
            ({"ecommerce", "retail", "shop", "store", "product"}, "product offer campaign with abandoned inquiry follow-up", ["weak product offer clarity", "low conversion", "no retargeting flow"], "First-order discount or buying guide"),
            ({"local service", "cleaning", "repair", "construction", "contractor"}, "local service lead campaign and quote request funnel", ["weak online trust", "no quote request funnel", "missing service pages"], "Free quote checklist"),
            ({"software", "it", "technology", "saas", "cloud", "cyber"}, "B2B demo and consultation campaign", ["long sales cycles", "unclear technical value", "low demo volume"], "Free automation audit"),
        ]
        for keywords, offer, pain_points, lead_magnet in profiles:
            if any(self._matches_keyword(text, keyword) for keyword in keywords):
                return {"offer": offer, "pain_points": pain_points, "lead_magnet": lead_magnet}
        return {
            "offer": "lead capture campaign and landing page",
            "pain_points": ["unclear offer", "weak call-to-action", "no follow-up process"],
            "lead_magnet": "Free growth checklist",
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
            "agency": "$75+/day with creative testing, lead capture optimization, and weekly reporting.",
        }

    def _facebook_ads(self, offer: str, audience: str, pain: str, location_phrase: str) -> list[Dict[str, str]]:
        return [
            {
                "primary_text": f"Still losing opportunities because of {pain}? Build a simple {offer}{location_phrase} that turns attention into real inquiries.",
                "headline": "Turn interest into qualified leads",
                "description": f"A practical {offer} for {audience}.",
                "cta": "Get a Free Review",
            },
            {
                "primary_text": f"Your next customer may already be searching. Make it easier for them to understand your offer, trust you, and take action.",
                "headline": "Launch a cleaner lead campaign",
                "description": "Clear offer, simple landing page, and follow-up built together.",
                "cta": "Request Review",
            },
        ]

    def _google_ads(self, offer: str, pain: str, location_phrase: str) -> list[Dict[str, str]]:
        return [
            {
                "headline_1": "Get More Qualified Leads",
                "headline_2": "Campaign Setup Service",
                "headline_3": "Free Growth Review",
                "description_1": f"Fix {pain} with a focused {offer}{location_phrase}.",
                "description_2": "Launch a simple campaign with landing page copy, ad copy, and follow-up strategy.",
            }
        ]

    def _reels_script(self, offer: str, pain: str) -> Dict[str, str]:
        return {
            "hook": f"Most businesses do not have a traffic problem. They have a {pain} problem.",
            "script": f"Show the current messy customer journey, then show how a {offer} makes the offer clear, captures the inquiry, and follows up before the lead goes cold.",
            "cta": "Comment 'review' or request a free campaign review.",
        }

    def _landing_page_copy(self, offer: str, pain_points: Sequence[str]) -> Dict[str, Any]:
        return {
            "headline": "Turn more clicks into qualified inquiries",
            "subheadline": f"A focused {offer} built to make your offer clear and easy to act on.",
            "bullets": [
                f"Reduce {pain_points[0]}",
                "Use clear campaign messaging across ads and landing pages",
                "Add a simple follow-up path for every inquiry",
            ],
            "cta": "Request a free review",
        }

    def _content_calendar(self, offer: str, audience: str) -> list[Dict[str, Any]]:
        ideas = [
            ("Problem awareness", "Show the most common reason leads do not convert.", "Request a free review"),
            ("Before and after", f"Explain how a {offer} changes the customer journey.", "See the campaign plan"),
            ("Trust builder", "Share proof points, testimonials, or a simple service process.", "Book a short call"),
            ("Offer clarity", f"Break down what is included in the {offer}.", "Ask for pricing"),
            ("FAQ post", f"Answer the top objections from {audience}.", "Send your question"),
            ("Quick tip", "Share one simple improvement people can make today.", "Save this idea"),
            ("Soft pitch", "Invite people to get a campaign review with no pressure.", "Request a review"),
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

    def _company_label(self, lead: Lead) -> str:
        for value in (lead.company_name, lead.company, lead.company_url, lead.website):
            normalized = str(value or "").strip()
            if normalized:
                if normalized.startswith(("http://", "https://")):
                    parsed = urlparse(normalized)
                    return parsed.netloc.replace("www.", "") or normalized
                return normalized
        return "this business"
