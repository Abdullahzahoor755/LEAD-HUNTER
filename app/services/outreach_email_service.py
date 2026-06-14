"""Rule-based outreach and follow-up email generation with optional AI refinement."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import re
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
            "services_offered": str(config.get("services_offered", "") or "").strip(),
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
        tone = str(config.get("tone", "") or "Professional").strip()
        goal = str(config.get("email_goal", "") or "Start conversation").strip()
        language = str(config.get("language", "") or "English").strip()
        sender_name = str(config.get("sender_name", "") or "").strip()
        signature = str(config.get("signature", "") or sender_name or config.get("brand_name", "") or "").strip()
        if sender_name and sender_name.lower() not in signature.lower():
            signature = f"{sender_name}\n{signature}".strip()
        personalization = self._personalization_engine(lead)
        cta = self._low_friction_cta(config)
        problem = self._business_problem()
        help_line = self._help_line()
        services_focus = self._single_service_focus(str(config.get("services_offered", "") or "").strip())
        if services_focus:
            help_line = f"{help_line} For you, I would keep it focused on {services_focus}."
        subject = f"Quick idea for {company}"[:90]
        body = (
            f"{personalization['personalized_observation']}\n\n"
            f"{problem}\n\n"
            f"{help_line}\n\n"
            f"{cta}"
        )
        if signature:
            body = f"{body}\n\n{signature}"
        score = self.human_score(subject, body)
        return {
            "subject": subject,
            "body": body,
            "mode": "fallback",
            "tone": tone,
            "email_goal": goal,
            "language": language,
            "company_summary": personalization["company_summary"],
            "likely_service_category": personalization["likely_service_category"],
            "personalized_observation": personalization["personalized_observation"],
            "human_score": score,
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
                "Just wanted to put this back near the top of your inbox.\n\n"
                f"{value_prop}\n\n"
                "Open to a quick 10-minute call?"
            ),
            2: (
                f"Hi {company} team,\n\n"
                "A simple place to start is looking at where new inquiries slow down after someone visits the site.\n\n"
                "I can share a short example of how we tighten that up. Worth seeing?"
            ),
            3: (
                f"Hi {company} team,\n\n"
                "I do not want to keep nudging if this is not relevant.\n\n"
                "Should I close the loop for now?"
            ),
        }
        score = self.human_score(subjects[sequence], bodies[sequence])
        return {"subject": subjects[sequence][:90], "body": bodies[sequence], "mode": "fallback", "human_score": score}

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
        score = self.human_score(subject, body)
        if score < 85:
            return {**fallback, "mode": "fallback", "ai_error": "AI provider output failed human quality gate"}
        return {
            "subject": subject[:100],
            "body": body,
            "mode": "ai_enhanced",
            "provider": str(status.get("provider", "") or "").strip(),
            "human_score": score,
            "company_summary": str(payload.get("company_summary", fallback.get("company_summary", "")) or "").strip(),
            "likely_service_category": str(payload.get("likely_service_category", fallback.get("likely_service_category", "")) or "").strip(),
            "personalized_observation": str(payload.get("personalized_observation", fallback.get("personalized_observation", "")) or "").strip(),
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
            "Rewrite this as human-written cold outreach from a founder, consultant, or agency owner.\n"
            "Rules: under 90 words. Four short lines: personalized observation, business problem, how we help, low-friction CTA.\n"
            "First sentence must start with I noticed, I saw, I came across, or I was looking at.\n"
            "Never mention search query, lead source, scraped data, target matching, AI generated content, or automation process.\n"
            "Do not list services. Focus on one problem and one outcome. Avoid corporate jargon and marketing fluff.\n"
            "If company data is thin, use soft personalization without hallucinating details.\n"
            "Return strict JSON only: {\"subject\":\"\", \"body\":\"\", \"company_summary\":\"\", \"likely_service_category\":\"\", \"personalized_observation\":\"\"}\n\n"
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
            "Refine this B2B follow-up email so it sounds manually written, short, helpful, and not pushy.\n"
            "Follow-up 1 is a friendly reminder. Follow-up 2 shares value. Follow-up 3 is a breakup email.\n"
            "Never mention search query, lead source, scraped data, target matching, AI generated content, or automation process.\n"
            "Avoid generic service lists and corporate jargon.\n"
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

    def _personalization_engine(self, lead: Lead) -> Dict[str, str]:
        category = self._likely_service_category(lead)
        summary = self._company_summary(lead, category)
        observation = self._personalized_observation(lead, summary, category)
        return {
            "company_summary": summary,
            "likely_service_category": category,
            "personalized_observation": observation,
        }

    def _company_summary(self, lead: Lead, category: str) -> str:
        company = self._company_label(lead)
        if lead.industry:
            return f"{company} appears to work in {lead.industry.strip()}."
        if lead.country:
            return f"{company} appears to serve customers in {lead.country.strip()}."
        domain = self._domain(lead.company_url or lead.website)
        if domain:
            return f"{company} has a public website at {domain}."
        return f"{company} appears to be a {category} provider."

    def _likely_service_category(self, lead: Lead) -> str:
        text = " ".join(
            str(value or "")
            for value in [lead.industry, lead.service_reason, lead.reason, lead.company, lead.company_name, lead.company_url]
        ).lower()
        if any(word in text for word in ("managed it", "cyber", "cloud", "software", "technology", "saas")):
            return "IT services"
        if any(word in text for word in ("real estate", "property", "broker")):
            return "real estate services"
        if any(word in text for word in ("school", "academy", "education", "training")):
            return "education services"
        if any(word in text for word in ("ecommerce", "retail", "shop", "store")):
            return "online retail"
        if lead.industry:
            return lead.industry.strip()[:80]
        return "service business"

    def _personalized_observation(self, lead: Lead, summary: str, category: str) -> str:
        company = self._company_label(lead)
        reason = self._lead_reason_fragment(lead)
        if reason:
            if reason.lower().startswith(("their ", "its ")):
                return f"I noticed {company}'s {reason.split(' ', 1)[1]}."
            return f"I noticed {company} {reason}."
        if lead.industry:
            return f"I noticed {company} works in {lead.industry.strip()}."
        if lead.company_url or lead.website:
            return f"I was looking at {company}'s website and saw you are active in {category}."
        if lead.country:
            return f"I came across {company} while researching {category} providers in {lead.country.strip()}."
        return f"I came across {company} while researching {category} providers."

    def _lead_reason_fragment(self, lead: Lead) -> str:
        reason = self._clean_ai_phrases(str(lead.service_reason or lead.reason or "").strip())
        if not reason:
            return ""
        reason = re.sub(r"^this company\s+", "", reason, flags=re.IGNORECASE)
        reason = re.sub(r"^appears to\s+", "appears to ", reason, flags=re.IGNORECASE)
        return reason.strip(" .")[:150]

    def _business_problem(self) -> str:
        return "A few good-fit prospects can slip away when there is not a simple next step."

    def _help_line(self) -> str:
        return "I help founders turn that interest into booked conversations with a focused lead capture and follow-up flow."

    def _low_friction_cta(self, config: Dict[str, Any]) -> str:
        configured = str(config.get("cta", "") or "").strip()
        blocked = ("discovery consultation", "synergies", "transform your business")
        if configured and not any(item in configured.lower() for item in blocked):
            if configured.endswith("?"):
                return configured
            return f"Open to {configured}?"
        return "If this sounds relevant, open to a quick 10-minute call?"

    def _single_service_focus(self, services: str) -> str:
        value = str(services or "").strip()
        if not value:
            return ""
        pieces = [piece.strip() for piece in re.split(r",|;|\n|\s+and\s+", value) if piece.strip()]
        if len(pieces) > 4:
            return ""
        return value[:120]

    def _clean_ai_phrases(self, value: str) -> str:
        text = str(value or "")
        replacements = {
            "matches the search query": "appears relevant",
            "target from search query": "business category",
            "target from the search query": "business category",
            "matches the technology target": "appears to offer technology services",
            "workflow automation may help": "a clearer follow-up process may help",
            "may need workflow automation": "may benefit from clearer follow-up",
        }
        for bad, good in replacements.items():
            text = re.sub(re.escape(bad), good, text, flags=re.IGNORECASE)
        return re.sub(r"\s+", " ", text).strip()

    def human_score(self, subject: str, body: str) -> int:
        text = f"{subject}\n{body}".lower()
        score = 100
        banned = (
            "ai generated",
            "search query",
            "lead source",
            "scraped data",
            "target matching",
            "target from search query",
            "target from the search query",
            "matches the technology target",
            "matches the search query",
            "we provide ai services",
            "i'm our team",
            "our ai detected",
            "we identified",
            "we analyzed",
            "automation process",
            "workflow automation may help",
            "may need workflow automation",
        )
        score -= 25 * sum(1 for phrase in banned if phrase in text)
        service_words = re.findall(r"\b(ai|websites?|chatbots?|automation|marketing|seo|apps?|crm|funnels?)\b", text)
        if len(set(service_words)) >= 5:
            score -= 25
        if any(phrase in text for phrase in ("let's discuss synergies", "discovery consultation", "transform your business")):
            score -= 20
        words = re.findall(r"\b[\w'-]+\b", body)
        if len(words) < 35 or len(words) > 90:
            score -= 10
        first_sentence = re.split(r"[.!?]", body.strip(), maxsplit=1)[0].lower()
        if not first_sentence.startswith(("i noticed", "i saw", "i came across", "i was looking at")):
            score -= 10
        if "?\n" not in body and not body.rstrip().endswith("?"):
            score -= 5
        return max(0, min(100, score))

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
