"""Rule-based outreach and follow-up email generation with optional AI refinement."""

from __future__ import annotations

import json
from typing import Any, Dict
from urllib.parse import urlparse

from app.core.models import Lead, TenantContext
from app.db.session import AsyncDatabaseSession, DatabaseSession
from app.services._async import maybe_await
from app.services.ai_provider_service import AIProviderService


UNSUBSCRIBE_FOOTER = "\n\nIf you prefer not to hear from us again, reply with unsubscribe."
AI_PLANS = {"pro", "agency"}


class OutreachEmailService:
    def __init__(self, db: DatabaseSession | AsyncDatabaseSession) -> None:
        self.db = db

    async def generate_outreach_email(self, tenant: TenantContext, lead: Lead) -> Dict[str, Any]:
        fallback = self._fallback_outreach(lead)
        return await self._maybe_ai_refine(
            tenant=tenant,
            fallback=fallback,
            system_prompt="You refine concise B2B cold outreach emails and return strict JSON.",
            user_prompt=self._outreach_refinement_prompt(lead, fallback),
        )

    async def generate_followup_email(
        self,
        tenant: TenantContext,
        lead: Lead,
        followup_number: int,
        previous_subject: str = "",
        previous_body: str = "",
    ) -> Dict[str, Any]:
        fallback = self._fallback_followup(lead, followup_number, previous_subject, previous_body)
        return await self._maybe_ai_refine(
            tenant=tenant,
            fallback=fallback,
            system_prompt="You refine concise B2B follow-up emails and return strict JSON.",
            user_prompt=self._followup_refinement_prompt(lead, followup_number, previous_subject, previous_body, fallback),
        )

    def ensure_unsubscribe_footer(self, body: str) -> str:
        normalized = str(body or "").rstrip()
        if UNSUBSCRIBE_FOOTER.strip().lower() in normalized.lower():
            return normalized
        return f"{normalized}{UNSUBSCRIBE_FOOTER}"

    def _fallback_outreach(self, lead: Lead) -> Dict[str, Any]:
        company = self._company_label(lead)
        observation = self._observation(lead)
        value_prop = self._value_prop(lead)
        subject = f"Quick idea for {company}"[:90]
        body = (
            f"Hi {company} team,\n\n"
            f"I noticed {observation}. {value_prop}\n\n"
            "Would it be worth a quick 10-minute chat to see if this could help?"
        )
        return {"subject": subject, "body": body, "mode": "fallback"}

    def _fallback_followup(
        self,
        lead: Lead,
        followup_number: int,
        previous_subject: str = "",
        previous_body: str = "",
    ) -> Dict[str, Any]:
        company = self._company_label(lead)
        value_prop = self._value_prop(lead)
        sequence = max(1, min(3, int(followup_number or 1)))
        subjects = {
            1: f"Following up on {company}",
            2: f"Useful idea for {company}",
            3: f"Should I close the loop?",
        }
        bodies = {
            1: (
                f"Hi {company} team,\n\n"
                f"Just following up on my note. {value_prop}\n\n"
                "Open to a quick chat this week?"
            ),
            2: (
                f"Hi {company} team,\n\n"
                "One practical starting point could be reviewing the current lead flow and finding the easiest automation win.\n\n"
                "Would a short audit be useful?"
            ),
            3: (
                f"Hi {company} team,\n\n"
                "I do not want to crowd your inbox, so I will close the loop here.\n\n"
                "If improving lead capture or follow-up becomes a priority, happy to share a few ideas."
            ),
        }
        return {"subject": subjects[sequence][:90], "body": bodies[sequence], "mode": "fallback"}

    async def _maybe_ai_refine(
        self,
        tenant: TenantContext,
        fallback: Dict[str, Any],
        system_prompt: str,
        user_prompt: str,
    ) -> Dict[str, Any]:
        if not await self._tenant_can_use_ai(tenant):
            return dict(fallback)
        ai_service = AIProviderService(self.db)
        try:
            status = await ai_service.status(tenant)
            refined = await ai_service.generate_text(
                tenant,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                json_mode=True,
                temperature=0.25,
                max_tokens=500,
            )
        except Exception:
            return {**fallback, "mode": "fallback", "ai_error": "AI provider failed; fallback used"}
        payload = self._coerce_ai_payload(refined)
        subject = str(payload.get("subject", "") or "").strip()
        body = str(payload.get("body", "") or "").strip()
        if not subject or not body:
            return {**fallback, "mode": "fallback", "ai_error": "AI provider failed; fallback used"}
        return {
            "subject": subject[:100],
            "body": body,
            "mode": "ai_enhanced",
            "provider": str(status.get("provider", "") or "").strip(),
        }

    async def _tenant_can_use_ai(self, tenant: TenantContext) -> bool:
        tenants = await maybe_await(self.db.tenants.list(tenant.tenant_id))
        if not tenants:
            return False
        plan = str(tenants[0].subscription_plan or "").strip().lower()
        if plan not in AI_PLANS:
            return False
        status = await AIProviderService(self.db).status(tenant)
        provider = str(status.get("provider", "fallback") or "fallback").strip().lower()
        return bool(status.get("configured")) and bool(status.get("enabled")) and provider != "fallback"

    def _coerce_ai_payload(self, value: dict | str) -> Dict[str, Any]:
        if isinstance(value, dict):
            return dict(value)
        try:
            parsed = json.loads(str(value or ""))
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}

    def _outreach_refinement_prompt(self, lead: Lead, fallback: Dict[str, Any]) -> str:
        return (
            "Refine this cold outreach email. Keep it under 120 words, professional, non-spammy, and specific.\n"
            "Preserve one observation, one value proposition, and one soft CTA.\n"
            "Return strict JSON only: {\"subject\":\"\", \"body\":\"\"}\n\n"
            f"Lead context: {json.dumps(self._lead_context(lead), ensure_ascii=True)}\n"
            f"Fallback email: {json.dumps({'subject': fallback.get('subject', ''), 'body': fallback.get('body', '')}, ensure_ascii=True)}"
        )

    def _followup_refinement_prompt(
        self,
        lead: Lead,
        followup_number: int,
        previous_subject: str,
        previous_body: str,
        fallback: Dict[str, Any],
    ) -> str:
        return (
            "Refine this B2B follow-up email. Keep it short, helpful, and not pushy.\n"
            "Return strict JSON only: {\"subject\":\"\", \"body\":\"\"}\n\n"
            f"Follow-up number: {followup_number}\n"
            f"Lead context: {json.dumps(self._lead_context(lead), ensure_ascii=True)}\n"
            f"Previous subject: {previous_subject}\n"
            f"Previous body: {previous_body[:700]}\n"
            f"Fallback follow-up: {json.dumps({'subject': fallback.get('subject', ''), 'body': fallback.get('body', '')}, ensure_ascii=True)}"
        )

    def _lead_context(self, lead: Lead) -> Dict[str, Any]:
        metadata = dict(lead.metadata or {})
        return {
            "company": self._company_label(lead),
            "company_url": lead.company_url or lead.website,
            "industry": lead.industry,
            "country": lead.country,
            "service_reason": lead.service_reason or lead.reason,
            "agency_kit": metadata.get("agency_kit", {}),
            "offer_match": metadata.get("offer_match", {}),
            "marketing_campaign_kit": metadata.get("marketing_campaign_kit", {}),
        }

    def _company_label(self, lead: Lead) -> str:
        value = str(lead.company or lead.company_name or "").strip()
        if value:
            return value[:80]
        domain = self._domain(lead.company_url or lead.website)
        return domain or "your team"

    def _observation(self, lead: Lead) -> str:
        metadata = dict(lead.metadata or {})
        for key in ("offer_match", "agency_kit", "marketing_campaign_kit"):
            payload = metadata.get(key)
            if isinstance(payload, dict):
                for field in ("summary", "recommended_offer", "recommended_service", "campaign_goal", "business_idea"):
                    value = str(payload.get(field, "") or "").strip()
                    if value:
                        return value[:160]
        if lead.service_reason:
            return str(lead.service_reason).strip()[:160]
        pieces = [piece for piece in (lead.industry, lead.country) if str(piece or "").strip()]
        if pieces:
            return f"you are active in {' / '.join(str(piece).strip() for piece in pieces)}"
        domain = self._domain(lead.company_url or lead.website)
        if domain:
            return f"your website at {domain} is already a useful first touchpoint"
        return "your business may benefit from a clearer lead capture and follow-up process"

    def _value_prop(self, lead: Lead) -> str:
        metadata = dict(lead.metadata or {})
        offer_match = metadata.get("offer_match", {})
        if isinstance(offer_match, dict):
            value = str(offer_match.get("recommended_offer", "") or offer_match.get("offer", "") or "").strip()
            if value:
                return f"We could help turn that into a simple system for more qualified conversations around {value}."
        agency_kit = metadata.get("agency_kit", {})
        if isinstance(agency_kit, dict):
            value = str(agency_kit.get("recommended_service", "") or agency_kit.get("service", "") or "").strip()
            if value:
                return f"We could help with {value} so more prospects move from interest to booked calls."
        if lead.industry:
            return f"We help {lead.industry.strip()} teams capture more qualified inquiries and follow up faster."
        return "We help teams capture more qualified inquiries and follow up faster without adding manual work."

    def _domain(self, url: str) -> str:
        value = str(url or "").strip()
        if not value:
            return ""
        parsed = urlparse(value if "://" in value else f"https://{value}")
        return parsed.netloc.replace("www.", "")[:80]
