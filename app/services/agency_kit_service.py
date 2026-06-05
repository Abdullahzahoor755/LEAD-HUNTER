"""Rule-based AI Agency Kit generation for tenant leads."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Sequence
from urllib.parse import urlparse

from app.core.models import Lead, Tenant, TenantContext
from app.db.session import AsyncDatabaseSession, DatabaseSession
from app.services._async import maybe_await


AGENCY_KIT_LIMITS = {
    "free": 3,
    "starter": 3,
    "pro": 100,
    "agency": 1000,
}


class AgencyKitLimitError(ValueError):
    """Raised when a tenant reaches the monthly agency kit limit."""


class AgencyKitService:
    def __init__(self, db: DatabaseSession | AsyncDatabaseSession) -> None:
        self.db = db

    async def generate_for_lead(self, tenant: TenantContext, lead_id: str) -> Dict[str, Any]:
        lead = await maybe_await(self.db.leads.get(tenant.tenant_id, lead_id))
        if lead is None:
            raise ValueError("Lead not found.")
        await self._consume_usage(tenant)
        kit = self.build_fallback_kit(lead)
        metadata = dict(lead.metadata or {})
        metadata["agency_kit"] = kit
        lead.metadata = metadata
        await maybe_await(self.db.for_tenant(tenant).save("leads", lead))
        return kit

    async def generate_bulk(self, tenant: TenantContext, lead_ids: Sequence[str]) -> Dict[str, Any]:
        generated: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for lead_id in lead_ids:
            normalized_id = str(lead_id or "").strip()
            if not normalized_id:
                continue
            try:
                kit = await self.generate_for_lead(tenant, normalized_id)
                generated.append({"lead_id": normalized_id, "agency_kit": kit})
            except AgencyKitLimitError:
                raise
            except Exception as error:
                errors.append({"lead_id": normalized_id, "error": str(error)})
        return {
            "tenant_id": tenant.tenant_id,
            "requested": len([item for item in lead_ids if str(item or "").strip()]),
            "generated_count": len(generated),
            "error_count": len(errors),
            "items": generated,
            "errors": errors,
        }

    def build_fallback_kit(self, lead: Lead) -> Dict[str, Any]:
        profile = self._service_profile(lead)
        contact_phone = self._phone_from_lead(lead)
        channel = self._recommended_channel(lead, contact_phone)
        company_label = self._company_label(lead)
        country = str(lead.country or lead.location or "").strip()
        snapshot = self._business_snapshot(lead, company_label, country, contact_phone)
        confidence_score = self._confidence_score(lead, contact_phone)
        recommended_service = profile["recommended_service"]
        offer_angle = self._offer_angle(profile, company_label, country)
        outreach_email = self._outreach_email(company_label, recommended_service, profile["pain_points"][0])
        call_script = self._call_script(company_label, recommended_service, profile["pain_points"][0])
        next_action = self._next_action(channel)
        return {
            "mode": "fallback",
            "business_snapshot": snapshot,
            "likely_pain_points": list(profile["pain_points"]),
            "recommended_service": recommended_service,
            "offer_angle": offer_angle,
            "recommended_channel": channel,
            "outreach_email": outreach_email,
            "whatsapp_or_call_script": call_script,
            "followup_sequence": self._followups(recommended_service),
            "proposal_outline": self._proposal_outline(recommended_service, profile["pain_points"][0]),
            "landing_page_copy": self._landing_page_copy(recommended_service, profile["pain_points"]),
            "confidence_score": confidence_score,
            "next_action": next_action,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    async def _consume_usage(self, tenant: TenantContext) -> None:
        tenant_record = await self._get_tenant(tenant.tenant_id)
        plan = str(tenant_record.subscription_plan or "Free").strip().lower()
        limit = AGENCY_KIT_LIMITS.get(plan, AGENCY_KIT_LIMITS["free"])
        period = datetime.now(timezone.utc).strftime("%Y-%m")
        settings = dict(tenant_record.settings or {})
        agency_usage = dict(settings.get("agency_kit_usage", {}))
        used = int(agency_usage.get(period, 0) or 0)
        if used >= limit:
            raise AgencyKitLimitError(f"Agency Kit monthly limit reached: {used}/{limit}.")
        agency_usage[period] = used + 1
        settings["agency_kit_usage"] = agency_usage
        tenant_record.settings = settings
        await maybe_await(self.db.tenants.save(tenant_record))

    async def _get_tenant(self, tenant_id: str) -> Tenant:
        tenants = await maybe_await(self.db.tenants.list(tenant_id))
        if not tenants:
            raise ValueError("Tenant does not exist.")
        return tenants[0]

    def _service_profile(self, lead: Lead) -> Dict[str, Any]:
        text = " ".join(
            [
                str(lead.industry or ""),
                str(lead.service_reason or ""),
                str(lead.company_summary or ""),
                str(lead.reason or ""),
                str((lead.metadata or {}).get("industry", "")),
            ]
        ).lower()
        profiles = [
            (
                {"software", "technology", "tech", "it ", "saas", "cloud", "cyber"},
                "Lead generation system + automation funnel",
                ["weak lead pipeline", "manual outreach", "no automated follow-up"],
            ),
            (
                {"restaurant", "food", "cafe", "catering", "dining"},
                "Google Maps optimization + WhatsApp ordering funnel",
                ["weak local visibility", "missing online ordering", "no review strategy"],
            ),
            (
                {"clinic", "health", "healthcare", "doctor", "dental", "medical"},
                "Appointment booking page + patient lead form",
                ["no booking funnel", "weak Google reviews", "missed patient inquiries"],
            ),
            (
                {"real estate", "property", "realtor", "broker"},
                "Property lead capture landing page + WhatsApp follow-up",
                ["leads lost from social media", "no follow-up automation", "weak landing pages"],
            ),
            (
                {"education", "training", "school", "academy", "course", "admissions"},
                "Admissions lead funnel + WhatsApp nurture sequence",
                ["inquiry leakage", "manual student follow-up", "weak conversion tracking"],
            ),
            (
                {"ecommerce", "e-commerce", "retail", "store", "shop", "product"},
                "Product landing page + abandoned inquiry follow-up",
                ["weak conversion", "no retargeting flow", "poor product offer clarity"],
            ),
            (
                {"construction", "contractor", "services", "local business", "repair", "cleaning"},
                "Local SEO + lead capture website",
                ["weak online trust", "no quote request funnel", "missing service pages"],
            ),
        ]
        for keywords, service, pain_points in profiles:
            if any(keyword in text for keyword in keywords):
                return {"recommended_service": service, "pain_points": pain_points}
        return {
            "recommended_service": "Website audit + local lead capture system",
            "pain_points": ["unclear offer", "weak call-to-action", "no automated follow-up"],
        }

    def _recommended_channel(self, lead: Lead, contact_phone: str) -> str:
        if str(lead.verified_email or lead.email or "").strip():
            return "email"
        if contact_phone:
            return "whatsapp"
        if str(lead.company_url or lead.website or "").strip():
            return "website_form"
        return "manual_research"

    def _confidence_score(self, lead: Lead, contact_phone: str) -> int:
        base = int(lead.score or lead.lead_score or 35)
        if str(lead.verified_email or lead.email or "").strip():
            base += 10
        if contact_phone:
            base += 8
        if str(lead.company_url or lead.website or "").strip():
            base += 5
        return max(35, min(100, base))

    def _phone_from_lead(self, lead: Lead) -> str:
        if str(lead.phone or "").strip():
            return str(lead.phone).strip()
        metadata = lead.metadata or {}
        for key in ("phone", "Phone", "phone_number", "contact_phone", "mobile", "whatsapp", "whatsapp_number"):
            value = str(metadata.get(key, "") or "").strip()
            if value:
                return value
        contact = metadata.get("contact")
        if isinstance(contact, dict):
            for key in ("phone", "mobile", "whatsapp"):
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
        return "your business"

    def _business_snapshot(self, lead: Lead, company_label: str, country: str, contact_phone: str) -> str:
        pieces = [f"{company_label} appears to be a {str(lead.industry or 'business').strip() or 'business'} lead"]
        if country:
            pieces.append(f"based in {country}")
        if str(lead.company_url or lead.website or "").strip():
            pieces.append("with an online presence")
        if str(lead.verified_email or lead.email or "").strip():
            pieces.append("and a reachable email contact")
        elif contact_phone:
            pieces.append("and a phone/WhatsApp contact")
        return " ".join(pieces) + "."

    def _offer_angle(self, profile: Dict[str, Any], company_label: str, country: str) -> str:
        location = f" in {country}" if country else ""
        return (
            f"Position the offer as a quick, low-friction improvement for {company_label}{location}: "
            f"set up {profile['recommended_service'].lower()} so more visitors become trackable inquiries."
        )

    def _outreach_email(self, company_label: str, recommended_service: str, primary_pain: str) -> str:
        return (
            "Subject: Quick idea for improving your lead flow\n\n"
            "Hi,\n\n"
            f"I came across {company_label} and noticed there may be an opportunity to improve {primary_pain}. "
            f"We help businesses set up a simple {recommended_service.lower()} so fewer potential customers are lost after the first visit.\n\n"
            "Would you be open to a quick 10-minute review?"
        )

    def _call_script(self, company_label: str, recommended_service: str, primary_pain: str) -> str:
        return (
            f"Hi, I was reviewing {company_label} and had a quick idea around {primary_pain}. "
            f"We build a practical {recommended_service.lower()} that helps capture and follow up with more inquiries. "
            "Would it be okay if I send a short review with 2-3 improvement ideas?"
        )

    def _followups(self, recommended_service: str) -> list[str]:
        return [
            "Follow-up 1: Just checking whether a quick lead-flow review would be useful this week.",
            f"Follow-up 2: The main value would be a practical {recommended_service.lower()} that makes inquiries easier to capture and follow up.",
            "Follow-up 3: No pressure if now is not the right time. I can send a short audit whenever it becomes useful.",
        ]

    def _proposal_outline(self, recommended_service: str, primary_pain: str) -> Dict[str, str]:
        return {
            "problem": f"The business may be losing opportunities because of {primary_pain}.",
            "solution": f"Build and launch a focused {recommended_service.lower()} with clear tracking and follow-up.",
            "timeline": "5-10 business days for the first usable version.",
            "pricing_angle": "Start with a fixed-scope starter package, then offer monthly optimization once results are visible.",
            "next_step": "Offer a quick audit and share 2-3 specific improvements before pitching the full project.",
        }

    def _landing_page_copy(self, recommended_service: str, pain_points: Sequence[str]) -> Dict[str, Any]:
        return {
            "headline": "Turn more visitors into qualified inquiries",
            "subheadline": f"A simple {recommended_service.lower()} designed to capture interest and follow up before leads go cold.",
            "bullets": [
                f"Reduce {pain_points[0]}",
                "Make the next step clear for every visitor",
                "Add a lightweight follow-up process for missed inquiries",
            ],
            "cta": "Request a quick review",
        }

    def _next_action(self, channel: str) -> str:
        return {
            "email": "Send the outreach email and follow up in 2-3 business days.",
            "whatsapp": "Send the WhatsApp script and offer a short audit.",
            "phone": "Call with the short script and ask permission to send a quick review.",
            "website_form": "Use the website contact form with the short email message.",
            "manual_research": "Find a decision-maker email or phone before sending outreach.",
        }.get(channel, "Review the lead manually before outreach.")
