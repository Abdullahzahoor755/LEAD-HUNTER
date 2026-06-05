"""Rule-based agency growth tools for tenant leads and agency planning."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from typing import Any, Dict, Sequence
from urllib.parse import urlparse

from app.core.models import Lead, TenantContext
from app.core.tenant import TenantIsolationError, assert_same_tenant
from app.db.session import AsyncDatabaseSession, DatabaseSession
from app.services.ai_provider_service import AIProviderNotConfigured, AIProviderService
from app.services._async import maybe_await


class AgencyGrowthService:
    def __init__(self, db: DatabaseSession | AsyncDatabaseSession) -> None:
        self.db = db

    async def lead_for_tenant(self, tenant: TenantContext, lead_id: str) -> Lead:
        lead = await maybe_await(self.db.leads.get(tenant.tenant_id, lead_id))
        if lead is None:
            raise ValueError("Lead not found.")
        return lead

    async def generate_offer_match(self, lead: Lead, tenant: TenantContext) -> Dict[str, Any]:
        self._assert_lead_tenant(lead, tenant)
        offer_match = self.build_offer_match(lead)
        offer_match = await self._enhance_with_ai(tenant, offer_match, "Lead-to-Offer Matchmaker")
        await self._save_metadata(tenant, lead, "offer_match", offer_match)
        return offer_match

    async def generate_whatsapp_sales_kit(self, lead: Lead, tenant: TenantContext) -> Dict[str, Any]:
        self._assert_lead_tenant(lead, tenant)
        sales_kit = self.build_whatsapp_sales_kit(lead)
        sales_kit = await self._enhance_with_ai(tenant, sales_kit, "WhatsApp Sales Script Generator")
        await self._save_metadata(tenant, lead, "whatsapp_sales_kit", sales_kit)
        return sales_kit

    async def generate_mini_agency_plan(self, payload: Dict[str, Any], tenant: TenantContext) -> Dict[str, Any]:
        plan = self.build_mini_agency_plan(payload)
        return await self._enhance_with_ai(tenant, plan, "Build My Mini Agency Mode")

    def build_mini_agency_plan(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        skill = str(payload.get("skill") or "other").strip().lower()
        target_country = str(payload.get("target_country") or "").strip()
        target_city = str(payload.get("target_city") or "").strip()
        daily_time = str(payload.get("daily_time") or "1 hour").strip()
        goal = str(payload.get("goal") or "first client").strip()
        preferred_niche = str(payload.get("preferred_niche") or "").strip()
        location = ", ".join(item for item in [target_city, target_country] if item) or "your target market"
        niches = self._mini_agency_niches(skill, preferred_niche)
        starter_offer = self._starter_offer(skill, niches[0])
        return {
            "mode": "fallback",
            "agency_positioning": self._agency_positioning(skill, location, goal, niches[0]),
            "best_niches": niches,
            "starter_offer": starter_offer,
            "pricing_suggestion": self._pricing_suggestion(skill),
            "lead_search_queries": self._lead_search_queries(skill, location, niches),
            "daily_roadmap": self._daily_roadmap(skill, daily_time, goal, niches[0]),
            "outreach_scripts": self._mini_agency_outreach_scripts(starter_offer, location),
            "proposal_template": self._proposal_template(starter_offer),
            "content_plan": self._content_plan(skill, niches[0]),
            "next_action": "Pick one niche, collect 25 leads, and send 10 friendly outreach messages today.",
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    async def _enhance_with_ai(self, tenant: TenantContext, payload: Dict[str, Any], feature_name: str) -> Dict[str, Any]:
        service = AIProviderService(self.db)
        try:
            enhanced = await service.generate_text(
                tenant,
                system_prompt=(
                    "You refine agency operating system JSON for freelancers. Keep the same object shape, "
                    "make it concise and practical, avoid spammy language, and return only valid JSON."
                ),
                user_prompt=(
                    f"Improve this {feature_name} output while preserving required fields:\n"
                    f"{json.dumps(payload, ensure_ascii=True)}"
                ),
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
        if isinstance(enhanced, dict):
            merged = dict(payload)
            merged.update(enhanced)
            merged["mode"] = "ai_enhanced"
            merged["provider"] = status.get("provider", "")
            return merged
        merged = dict(payload)
        merged["mode"] = "ai_enhanced"
        merged["provider"] = status.get("provider", "")
        merged["ai_notes"] = str(enhanced)
        return merged

    def build_offer_match(self, lead: Lead) -> Dict[str, Any]:
        profile = self._offer_profile(lead)
        phone = self._phone_from_lead(lead)
        company_label = self._company_label(lead)
        country = str(lead.country or lead.location or "").strip()
        channel = self._best_channel(lead, phone)
        confidence = self._confidence_score(lead, phone, bool(profile.get("matched")))
        starter_deliverables, pro_deliverables = self._package_deliverables(profile["recommended_offer"])
        location_phrase = f" in {country}" if country else ""
        return {
            "mode": "fallback",
            "recommended_offer": profile["recommended_offer"],
            "offer_category": profile["offer_category"],
            "why_this_offer": (
                f"{company_label}{location_phrase} looks like a fit because {profile['why']} "
                "This offer is practical, easy to explain, and tied to visible lead-flow problems."
            ),
            "business_pain": list(profile["business_pain"]),
            "starter_package": {
                "name": f"Starter {profile['package_name']}",
                "price_range": "$150-$500",
                "deliverables": starter_deliverables,
            },
            "pro_package": {
                "name": f"Pro {profile['package_name']}",
                "price_range": "$500-$1,500+",
                "deliverables": pro_deliverables,
            },
            "pitch_angle": (
                f"Offer {profile['recommended_offer'].lower()} as a small, low-risk fix that helps "
                f"{company_label} capture and follow up with more qualified inquiries."
            ),
            "best_channel": channel,
            "confidence_score": confidence,
            "next_step": self._offer_next_step(channel),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    def build_whatsapp_sales_kit(self, lead: Lead) -> Dict[str, Any]:
        metadata = dict(lead.metadata or {})
        offer_match = metadata.get("offer_match") if isinstance(metadata.get("offer_match"), dict) else {}
        agency_kit = metadata.get("agency_kit") if isinstance(metadata.get("agency_kit"), dict) else {}
        offer = str(
            offer_match.get("recommended_offer")
            or agency_kit.get("recommended_service")
            or self._offer_profile(lead)["recommended_offer"]
        )
        pain_points = offer_match.get("business_pain") if isinstance(offer_match.get("business_pain"), list) else []
        primary_pain = str(pain_points[0] if pain_points else self._offer_profile(lead)["business_pain"][0])
        company_label = self._company_label(lead)
        channel = self._sales_channel(lead)
        return {
            "mode": "fallback",
            "recommended_channel": channel,
            "whatsapp_opener": (
                f"Hi, I was checking {company_label} and noticed a quick opportunity around {primary_pain}. "
                f"I help local businesses set up a simple {offer.lower()} so inquiries are easier to capture and follow up. "
                "Would it be okay if I send 2 quick ideas?"
            ),
            "followup_1": "Just checking if a short lead-flow review would be useful this week. No pressure.",
            "followup_2": (
                f"The main idea is a practical {offer.lower()} that keeps the offer clear and makes follow-up easier."
            ),
            "followup_3": "If now is not the right time, I can send a short checklist for later.",
            "voice_note_script": (
                f"Hi, quick voice note. I saw {company_label} and had a simple idea to improve {primary_pain}. "
                f"It is basically a {offer.lower()} with clear call-to-action and follow-up. "
                "I can send a short example if useful."
            ),
            "call_script": {
                "opening": f"Hi, I am calling with a quick idea for {company_label}. Is this a bad time?",
                "problem_question": f"Are you currently happy with how new inquiries are captured and followed up?",
                "value_pitch": (
                    f"We set up a simple {offer.lower()} so more interested people become trackable inquiries."
                ),
                "soft_close": "Would it be okay if I send a 1-page review with 2-3 improvement ideas?",
            },
            "objection_replies": {
                "not_interested": "Totally understood. I will not push. I can still send a quick checklist if useful later.",
                "send_details": "Sure, I will send a short summary with the problem, the fix, and a simple starter price.",
                "price_question": "It depends on scope, but starter setups are usually small fixed projects before any monthly work.",
                "already_have_vendor": "That is good. This can still be useful as a second opinion on lead capture and follow-up.",
            },
            "next_step": self._sales_next_step(channel),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    async def _save_metadata(self, tenant: TenantContext, lead: Lead, key: str, value: Dict[str, Any]) -> None:
        metadata = dict(lead.metadata or {})
        metadata[key] = value
        lead.metadata = metadata
        await maybe_await(self.db.for_tenant(tenant).save("leads", lead))

    def _assert_lead_tenant(self, lead: Lead, tenant: TenantContext) -> None:
        try:
            assert_same_tenant(tenant.tenant_id, lead.tenant_id)
        except TenantIsolationError as error:
            raise ValueError("Lead not found.") from error

    def _offer_profile(self, lead: Lead) -> Dict[str, Any]:
        text = self._lead_text(lead)
        profiles = [
            (
                {"restaurant", "food", "cafe", "catering", "dining", "menu"},
                "Google Maps optimization + WhatsApp ordering funnel",
                "google_maps",
                ["weak local visibility", "missed WhatsApp orders", "low repeat customer follow-up"],
                "Google Maps + WhatsApp Funnel",
                "food businesses win when nearby customers can find them, trust them, and order quickly.",
            ),
            (
                {"clinic", "healthcare", "health", "doctor", "dental", "medical", "patient"},
                "Appointment booking page + patient lead form",
                "appointment_funnel",
                ["missed appointment inquiries", "unclear service pages", "weak review trust"],
                "Appointment Funnel",
                "clinics need a simple path from interest to booked appointment.",
            ),
            (
                {"real estate", "property", "realtor", "broker", "housing"},
                "Property lead capture landing page + WhatsApp follow-up",
                "whatsapp_funnel",
                ["property inquiries going cold", "slow follow-up", "weak landing pages"],
                "Property Lead Funnel",
                "property leads need fast follow-up and a clear way to request details.",
            ),
            (
                {"education", "training", "school", "academy", "course", "admissions"},
                "Admissions lead funnel + WhatsApp nurture sequence",
                "crm",
                ["student inquiry leakage", "manual follow-up", "unclear admissions offer"],
                "Admissions Funnel",
                "education leads need trust, reminders, and a clear admissions next step.",
            ),
            (
                {"ecommerce", "e-commerce", "retail", "store", "shop", "product"},
                "Product landing page + conversion follow-up",
                "marketing_campaign",
                ["weak product offer clarity", "low conversion", "missing follow-up"],
                "Product Conversion Funnel",
                "retail buyers respond better when the offer and next step are obvious.",
            ),
            (
                {"software", "technology", "tech", "it", "saas", "cloud", "cyber"},
                "Lead generation system + CRM automation",
                "automation",
                ["weak B2B lead pipeline", "manual sales tracking", "low demo volume"],
                "B2B Lead System",
                "software and IT companies need a repeatable way to capture and manage qualified demos.",
            ),
            (
                {"immigration", "visa", "consultancy", "consultant"},
                "Consultation booking funnel + lead follow-up CRM",
                "appointment_funnel",
                ["missed consultation requests", "low trust signals", "manual follow-up"],
                "Consultation Funnel",
                "consultancies sell trust, eligibility clarity, and fast response time.",
            ),
            (
                {"cleaning", "repair", "construction", "contractor", "plumber", "electrician", "local service"},
                "Local SEO + quote request website",
                "local_seo",
                ["weak local search presence", "missing quote request flow", "low online trust"],
                "Local SEO + Quote Funnel",
                "local services need quote requests from nearby buyers with urgent needs.",
            ),
        ]
        for keywords, offer, category, pains, package_name, why in profiles:
            if any(self._matches_keyword(text, keyword) for keyword in keywords):
                return {
                    "matched": True,
                    "recommended_offer": offer,
                    "offer_category": category,
                    "business_pain": pains,
                    "package_name": package_name,
                    "why": why,
                }
        return {
            "matched": False,
            "recommended_offer": "Website audit + lead capture system",
            "offer_category": "website",
            "business_pain": ["unclear online offer", "weak call-to-action", "no simple follow-up process"],
            "package_name": "Website Audit + Lead Capture",
            "why": "most businesses can benefit from a clearer offer, stronger trust signals, and a simple inquiry path.",
        }

    def _lead_text(self, lead: Lead) -> str:
        metadata = dict(lead.metadata or {})
        agency_kit = metadata.get("agency_kit") if isinstance(metadata.get("agency_kit"), dict) else {}
        campaign = metadata.get("marketing_campaign_kit") if isinstance(metadata.get("marketing_campaign_kit"), dict) else {}
        parts = [
            lead.industry,
            lead.service_reason,
            lead.reason,
            lead.company_summary,
            metadata.get("industry", ""),
            agency_kit.get("recommended_service", ""),
            campaign.get("business_idea", ""),
            campaign.get("campaign_goal", ""),
        ]
        return " ".join(str(item or "") for item in parts).lower()

    def _matches_keyword(self, text: str, keyword: str) -> bool:
        normalized = str(keyword or "").strip().lower()
        if not normalized:
            return False
        if len(normalized) <= 3:
            return bool(re.search(rf"\b{re.escape(normalized)}\b", text))
        return normalized in text

    def _package_deliverables(self, offer: str) -> tuple[list[str], list[str]]:
        return (
            [
                "Lead-flow audit",
                f"One-page setup plan for {offer.lower()}",
                "Basic copy and call-to-action recommendations",
            ],
            [
                "Complete funnel setup plan",
                "Landing page or profile optimization copy",
                "Follow-up scripts and simple CRM workflow",
                "7-day launch checklist",
            ],
        )

    def _best_channel(self, lead: Lead, phone: str) -> str:
        if phone:
            return "whatsapp"
        if str(lead.verified_email or lead.email or "").strip():
            return "email"
        if str(lead.company_url or lead.website or "").strip():
            return "website_form"
        return "manual_research"

    def _sales_channel(self, lead: Lead) -> str:
        phone = self._phone_from_lead(lead)
        if phone:
            return "whatsapp"
        if str(lead.verified_email or lead.email or "").strip():
            return "email"
        if str(lead.company_url or lead.website or "").strip():
            return "website_form"
        return "manual_research"

    def _confidence_score(self, lead: Lead, phone: str, matched_profile: bool) -> int:
        base = int(lead.score or lead.lead_score or 35)
        if matched_profile:
            base += 12
        if phone:
            base += 8
        if str(lead.verified_email or lead.email or "").strip():
            base += 8
        if str(lead.company_url or lead.website or "").strip():
            base += 5
        return max(30, min(100, base))

    def _offer_next_step(self, channel: str) -> str:
        if channel == "whatsapp":
            return "Send a short WhatsApp opener and offer a quick 2-point lead-flow review."
        if channel == "email":
            return "Send a concise email with one visible improvement idea and a soft review CTA."
        if channel == "website_form":
            return "Use the website form to request permission to send a short audit."
        return "Find a direct phone, WhatsApp, or email contact before pitching."

    def _sales_next_step(self, channel: str) -> str:
        if channel == "whatsapp":
            return "Send the opener, wait at least one business day, then use follow-up 1."
        if channel == "email":
            return "Adapt the opener into email format and keep the CTA to one simple reply."
        if channel == "website_form":
            return "Send a short website-form message asking where to share the review."
        return "Research a direct contact before sending the script."

    def _phone_from_lead(self, lead: Lead) -> str:
        if str(lead.phone or "").strip():
            return str(lead.phone).strip()
        metadata = dict(lead.metadata or {})
        for key in ("phone", "Phone", "phone_number", "contact_phone", "mobile", "whatsapp", "whatsapp_number"):
            value = str(metadata.get(key, "") or "").strip()
            if value:
                return value
        contact = metadata.get("contact")
        if isinstance(contact, dict):
            for key in ("phone", "mobile", "whatsapp", "whatsapp_number"):
                value = str(contact.get(key, "") or "").strip()
                if value:
                    return value
        return ""

    def _company_label(self, lead: Lead) -> str:
        for value in (lead.company_name, lead.company, lead.company_url, lead.website):
            normalized = str(value or "").strip()
            if normalized:
                if normalized.startswith(("http://", "https://")):
                    parsed = urlparse(normalized)
                    return parsed.netloc.replace("www.", "") or normalized
                return normalized
        return "this business"

    def _mini_agency_niches(self, skill: str, preferred_niche: str) -> list[str]:
        if preferred_niche:
            return [preferred_niche, "restaurants and cafes", "clinics and local services"]
        mapping = {
            "web design": ["clinics", "real estate agents", "local service businesses"],
            "seo": ["restaurants and cafes", "local contractors", "clinics"],
            "social media": ["restaurants and cafes", "beauty salons", "fitness coaches"],
            "automation": ["consultancies", "real estate teams", "training institutes"],
            "lead generation": ["software companies", "real estate agents", "local services"],
            "marketing": ["ecommerce stores", "restaurants", "education providers"],
        }
        return mapping.get(skill, ["local services", "clinics", "restaurants and cafes"])

    def _starter_offer(self, skill: str, niche: str) -> str:
        offers = {
            "web design": f"One-page lead capture website for {niche}",
            "seo": f"Local SEO and Google profile improvement for {niche}",
            "social media": f"7-day content and ad starter kit for {niche}",
            "automation": f"Inquiry follow-up automation for {niche}",
            "lead generation": f"Lead list + outreach starter system for {niche}",
            "marketing": f"Simple campaign kit and landing page copy for {niche}",
        }
        return offers.get(skill, f"Website audit + lead capture system for {niche}")

    def _agency_positioning(self, skill: str, location: str, goal: str, niche: str) -> str:
        return (
            f"Position yourself as a focused {skill or 'digital'} helper for {niche} in {location}. "
            f"The first goal is {goal}: sell a small, clear outcome before pitching monthly retainers."
        )

    def _pricing_suggestion(self, skill: str) -> Dict[str, str]:
        if skill in {"automation", "lead generation"}:
            return {"starter": "$200-$500 setup", "pro": "$700-$1,500 setup", "monthly": "$300-$1,000/month"}
        if skill in {"web design", "seo", "marketing"}:
            return {"starter": "$150-$400 setup", "pro": "$500-$1,200 setup", "monthly": "$250-$800/month"}
        return {"starter": "$100-$300 setup", "pro": "$400-$900 setup", "monthly": "$200-$600/month"}

    def _lead_search_queries(self, skill: str, location: str, niches: Sequence[str]) -> list[str]:
        return [
            f"{niches[0]} in {location}",
            f"{niches[1]} contact email {location}",
            f"{niches[2]} WhatsApp {location}",
            f"{skill} opportunities for {niches[0]} {location}",
        ]

    def _daily_roadmap(self, skill: str, daily_time: str, goal: str, niche: str) -> list[Dict[str, Any]]:
        actions = [
            ("Pick niche", ["Choose one niche", "Write one starter offer", "Define a simple proof checklist"], "Niche and offer are written"),
            ("Build lead list", ["Collect 25 leads", "Save company URL and contact", "Score each lead 1-5"], "25 leads collected"),
            ("Create audit template", ["Write 5 audit points", "Prepare before/after examples", "Create a 1-page proposal outline"], "Reusable audit ready"),
            ("Send first outreach", ["Send 10 friendly messages", "Track replies", "Do not pitch too hard"], "10 messages sent"),
            ("Follow up", ["Follow up with non-replies", "Improve opener from replies", "Book one review call"], "1 call or positive reply"),
            ("Package offer", ["Turn feedback into starter package", "Set starter/pro/monthly prices", "Prepare delivery checklist"], "Offer page ready"),
            ("Second batch", ["Send 15 more messages", "Use best-performing angle", "Ask for a short call"], "25 total messages sent"),
            ("Create proof asset", ["Publish a mini case study", "Share one audit insight", "Add credibility screenshots"], "1 proof post live"),
            ("Improve lead source", ["Search a second city", "Collect 25 more leads", "Tag by urgency"], "50 leads total"),
            ("Sales calls", ["Run review calls", "Ask discovery questions", "Offer starter package"], "1 proposal sent"),
            ("Delivery prep", ["Prepare templates", "Create onboarding form", "Define 3-day delivery process"], "Delivery process ready"),
            ("Close follow-ups", ["Follow up proposals", "Offer a smaller starter audit", "Ask for decision timeline"], "Decision timeline known"),
            ("Monthly upsell", ["Define monthly maintenance", "Write reporting promise", "Create renewal checklist"], "Monthly offer ready"),
            ("Review and repeat", ["Review metrics", "Keep best niche", "Plan next 50 leads"], f"Next {goal} action chosen"),
        ]
        return [
            {
                "day": index,
                "focus": focus,
                "tasks": [f"{task} ({daily_time})" if task == tasks[0] else task for task in tasks],
                "success_metric": metric,
            }
            for index, (focus, tasks, metric) in enumerate(actions, start=1)
        ]

    def _mini_agency_outreach_scripts(self, starter_offer: str, location: str) -> Dict[str, str]:
        return {
            "whatsapp": (
                f"Hi, I am helping businesses in {location} improve lead capture. "
                f"I had a quick idea around a {starter_offer.lower()}. Can I send 2 short suggestions?"
            ),
            "email": (
                "Subject: Quick lead-flow idea\n\n"
                f"Hi, I noticed a simple opportunity to improve inquiries with a {starter_offer.lower()}. "
                "Would you be open to a short review?"
            ),
            "followup": "Just checking if the short review would be useful. Happy to send it with no pressure.",
        }

    def _proposal_template(self, starter_offer: str) -> Dict[str, str]:
        return {
            "problem": "The business may be getting attention but losing inquiries because the offer and next step are unclear.",
            "solution": f"Set up a practical {starter_offer.lower()} with clear copy, CTA, and follow-up.",
            "timeline": "3-7 days for the starter version.",
            "price_anchor": "Start with a fixed setup fee before offering monthly optimization.",
            "next_step": "Approve the starter scope and share access/content needed for setup.",
        }

    def _content_plan(self, skill: str, niche: str) -> list[Dict[str, str]]:
        return [
            {
                "post": "Problem post",
                "caption": f"Most {niche} do not need more tools. They need a clearer path from visitor to inquiry.",
                "cta": "Ask for a quick review",
            },
            {
                "post": "Before/after post",
                "caption": f"Show how one {skill or 'digital'} improvement can make the next step easier for customers.",
                "cta": "Save this checklist",
            },
            {
                "post": "Offer post",
                "caption": f"I am opening a few starter slots for {niche} that want a cleaner lead capture setup.",
                "cta": "Message 'review'",
            },
        ]
