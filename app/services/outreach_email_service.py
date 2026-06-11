"""Rule-based outreach and follow-up email generation with optional AI refinement."""

from __future__ import annotations

from datetime import datetime, timezone
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
        config = await self.personalization_config(tenant)
        fallback = self._fallback_outreach(lead, config)
        return await self._maybe_ai_refine(
            tenant=tenant,
            fallback=fallback,
            system_prompt="You refine concise B2B cold outreach emails and return strict JSON.",
            user_prompt=self._outreach_refinement_prompt(lead, fallback, config),
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

    async def personalization_config(self, tenant: TenantContext) -> Dict[str, Any]:
        tenants = await maybe_await(self.db.tenants.list(tenant.tenant_id))
        settings = dict(tenants[0].settings or {}) if tenants else {}
        config = dict(settings.get("email_personalization", {}) or {})
        return {
            "sender_name": str(config.get("sender_name", "") or "").strip(),
            "brand_name": str(config.get("brand_name", "") or "").strip(),
            "services_offered": str(config.get("services_offered", "") or "AI lead generation, website automation, chatbot development, marketing automation").strip(),
            "target_customer_type": str(config.get("target_customer_type", "") or "businesses").strip(),
            "tone": str(config.get("tone", "") or "Professional").strip(),
            "email_goal": str(config.get("email_goal", "") or "Start conversation").strip(),
            "cta": str(config.get("cta", "") or "a quick 10-minute call this week").strip(),
            "language": str(config.get("language", "") or "English").strip(),
            "signature": str(config.get("signature", "") or "").strip(),
            "updated_at": str(config.get("updated_at", "") or "").strip(),
        }

    async def save_personalization_config(self, tenant: TenantContext, config: Dict[str, Any]) -> Dict[str, Any]:
        tenants = await maybe_await(self.db.tenants.list(tenant.tenant_id))
        if not tenants:
            raise ValueError("Tenant does not exist.")
        tenant_record = tenants[0]
        settings = dict(tenant_record.settings or {})
        settings["email_personalization"] = {
            "sender_name": str(config.get("sender_name", "") or "").strip()[:120],
            "brand_name": str(config.get("brand_name", "") or "").strip()[:120],
            "services_offered": str(config.get("services_offered", "") or "").strip()[:500],
            "target_customer_type": str(config.get("target_customer_type", "") or "").strip()[:180],
            "tone": str(config.get("tone", "") or "Professional").strip()[:60],
            "email_goal": str(config.get("email_goal", "") or "Start conversation").strip()[:80],
            "cta": str(config.get("cta", "") or "").strip()[:220],
            "language": str(config.get("language", "") or "English").strip()[:80],
            "signature": str(config.get("signature", "") or "").strip()[:300],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        tenant_record.settings = settings
        await maybe_await(self.db.tenants.save(tenant_record))
        return await self.personalization_config(tenant)

    async def generate_sample_email(self, tenant: TenantContext, lead: Lead | None = None, config: Dict[str, Any] | None = None) -> Dict[str, Any]:
        config = dict(config or await self.personalization_config(tenant))
        sample = lead or Lead(
            tenant_id=tenant.tenant_id,
            company="Sample Software Co",
            company_url="https://example.com",
            industry="Software & IT",
            service_reason="The company sells services online and may benefit from better lead capture and follow-up.",
        )
        return self._fallback_outreach(sample, config)

    def _fallback_outreach(self, lead: Lead, config: Dict[str, Any] | None = None) -> Dict[str, Any]:
        config = dict(config or {})
        company = self._company_label(lead)
        sender_name = str(config.get("sender_name", "") or "").strip()
        brand_name = str(config.get("brand_name", "") or "our team").strip()
        services = str(config.get("services_offered", "") or self._value_prop(lead)).strip()
        target = str(config.get("target_customer_type", "") or "businesses").strip()
        tone = str(config.get("tone", "") or "Professional").strip()
        goal = str(config.get("email_goal", "") or "Start conversation").strip()
        language = str(config.get("language", "") or "English").strip()
        cta = str(config.get("cta", "") or "a quick 10-minute call this week").strip()
        signature = str(config.get("signature", "") or sender_name or brand_name).strip()
        personalized_line = self._personalized_line(lead)
        subject = f"Quick idea for {company}"[:90]
        context_note = ""
        if tone or goal or language:
            context_note = f"\nTone: {tone}. Goal: {goal}. Language: {language}."
        body = (
            f"Hi {company} team,\n\n"
            f"I'm {sender_name + ' from ' if sender_name else ''}{brand_name}. "
            f"We help {target} with {services}.\n\n"
            f"{personalized_line}\n\n"
            f"Would you be open to {cta}?\n\n"
            f"Best,\n{signature}"
            f"{context_note if context_note and language.lower() != 'english' else ''}"
        )
        return {
            "subject": subject,
            "body": body,
            "mode": "fallback",
            "tone": tone,
            "email_goal": goal,
            "language": language,
        }

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

    def _outreach_refinement_prompt(self, lead: Lead, fallback: Dict[str, Any], config: Dict[str, Any]) -> str:
        return (
            "Refine this cold outreach email. Keep it under 120 words, professional, non-spammy, and specific.\n"
            "Use the sender name, brand/company name, services, target customer type, tone, email goal, CTA, and language from the sender configuration.\n"
            "Use the lead company name, website/domain, and business description when available.\n"
            "Avoid fake claims, spammy wording, scraping mentions, or saying AI found their website. Avoid saying 'I noticed' unless supported by lead context.\n"
            "Return strict JSON only: {\"subject\":\"\", \"body\":\"\"}\n\n"
            f"Sender configuration: {json.dumps(config, ensure_ascii=True)}\n"
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

    def _personalized_line(self, lead: Lead) -> str:
        reason = str(lead.service_reason or lead.reason or "").strip()
        if reason:
            return reason[:180]
        if lead.industry:
            return f"For {lead.industry.strip()} teams, a simpler lead capture and follow-up system can often turn more inquiries into conversations."
        return "A practical starting point could be improving lead capture, response speed, and follow-up consistency."

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
