"""Lead service with explicit tenant isolation."""

from __future__ import annotations

from dataclasses import fields
from datetime import datetime, timezone
import re
from typing import Any, Sequence
from urllib.parse import quote
from urllib.parse import urlparse

from app.core.models import Lead, TenantContext
from app.core.tenant import assert_same_tenant
from app.db.session import AsyncDatabaseSession, DatabaseSession
from app.services._async import maybe_await


class LeadService:
    COUNTRY_ALIASES = {
        "saudi arabia": "Saudi Arabia",
        "kingdom of saudi arabia": "Saudi Arabia",
        "ksa": "Saudi Arabia",
        "uae": "UAE",
        "united arab emirates": "UAE",
        "qatar": "Qatar",
        "kuwait": "Kuwait",
        "bahrain": "Bahrain",
        "oman": "Oman",
        "pakistan": "Pakistan",
    }
    COUNTRY_ORDER = tuple(COUNTRY_ALIASES.values())
    PUBLIC_EMAIL_DOMAINS = {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "proton.me", "icloud.com"}
    INDUSTRY_MAP = {
        "software": "Software & IT",
        "it": "Software & IT",
        "technology": "Software & IT",
        "saas": "Software & IT",
        "health": "Healthcare",
        "medical": "Healthcare",
        "clinic": "Healthcare",
        "real estate": "Real Estate",
        "property": "Real Estate",
        "construction": "Construction",
        "builder": "Construction",
        "manufacturing": "Manufacturing",
        "factory": "Manufacturing",
        "logistics": "Logistics",
        "shipping": "Logistics",
        "transport": "Logistics",
        "finance": "Finance",
        "bank": "Finance",
        "insurance": "Finance",
        "education": "Education",
        "school": "Education",
        "university": "Education",
        "retail": "Retail",
        "shop": "Retail",
        "hospitality": "Hospitality",
        "hotel": "Hospitality",
        "restaurant": "Hospitality",
        "marketing": "Marketing",
        "agency": "Marketing",
        "legal": "Legal",
        "law": "Legal",
        "energy": "Energy",
        "oil": "Energy",
        "gas": "Energy",
        "government": "Government",
        "public sector": "Government",
    }
    GENERIC_EMAIL_PREFIXES = ("info@", "admin@", "contact@", "sales@", "support@", "hello@", "enquiries@", "inquiries@")

    def __init__(self, db: DatabaseSession | AsyncDatabaseSession) -> None:
        self.db = db

    async def list_leads(self, tenant: TenantContext) -> Sequence[Lead]:
        return await maybe_await(self.db.for_tenant(tenant).list("leads"))

    async def lead_for_tenant(self, tenant: TenantContext, lead_id: str) -> Lead:
        lead = await maybe_await(self.db.for_tenant(tenant).get("leads", lead_id))
        if lead is None:
            raise ValueError("Lead not found.")
        assert_same_tenant(tenant.tenant_id, lead.tenant_id)
        return lead

    async def upsert_lead(self, tenant: TenantContext, lead: Lead) -> Lead:
        assert_same_tenant(tenant.tenant_id, lead.tenant_id)
        lead = self._normalize_lead(lead)
        existing = await maybe_await(self.db.leads.find_by_company_url(tenant.tenant_id, lead.company_url)) if lead.company_url else None
        if existing is None and lead.verified_email:
            existing = await maybe_await(self.db.leads.find_by_email(tenant.tenant_id, lead.verified_email))
        if existing:
            assert_same_tenant(tenant.tenant_id, existing.tenant_id)
            existing.company_url = lead.company_url or existing.company_url
            existing.verified_email = self._merge_verified_email(existing, lead)
            existing.service_reason = self._merge_service_reason(existing, lead)
            existing.outreach_status = lead.outreach_status or existing.outreach_status
            existing.followup_count = lead.followup_count if lead.followup_count is not None else existing.followup_count
            existing.reply_status = lead.reply_status or existing.reply_status
            existing.last_reply_at = lead.last_reply_at or existing.last_reply_at
            existing.company = lead.company or existing.company
            existing.website = lead.website or existing.website
            existing.email = self._merge_email(existing, lead)
            existing.phone = lead.phone or existing.phone
            existing.location = lead.location or existing.location
            existing.country = lead.country or existing.country
            existing.industry = lead.industry or existing.industry
            existing.score = lead.score if lead.score else existing.score
            existing.reason = lead.reason or existing.reason
            existing.status = lead.status or existing.status
            existing.source_query = lead.source_query or existing.source_query
            existing.metadata.update(lead.metadata)
            return await maybe_await(self.db.for_tenant(tenant).save("leads", existing))
        return await maybe_await(self.db.for_tenant(tenant).save("leads", lead))

    def _merge_service_reason(self, existing: Lead, lead: Lead) -> str:
        if lead.service_reason:
            return lead.service_reason
        if self._looks_like_score_breakdown(existing.service_reason):
            return ""
        return existing.service_reason

    def _merge_verified_email(self, existing: Lead, lead: Lead) -> str:
        if lead.verified_email:
            return lead.verified_email
        if self._stored_email_is_rejected(existing, lead):
            return ""
        return existing.verified_email

    def _merge_email(self, existing: Lead, lead: Lead) -> str:
        if lead.email:
            return lead.email
        if self._stored_email_is_rejected(existing, lead):
            return ""
        return existing.email

    def _stored_email_is_rejected(self, existing: Lead, lead: Lead) -> bool:
        stale_email = self._normalize_email(existing.verified_email or existing.email)
        if not stale_email:
            return False
        rejected = lead.metadata.get("rejected_emails", []) if isinstance(lead.metadata, dict) else []
        return any(
            isinstance(item, dict)
            and str(item.get("email", "")).strip().lower() == stale_email
            and str(item.get("reason", "")).strip() in {"domain_mismatch", "suspicious_email_domain"}
            for item in rejected
        )

    def _normalize_lead(self, lead: Lead) -> Lead:
        normalized = Lead(**{field.name: getattr(lead, field.name) for field in fields(Lead)})
        normalized.company_url = self._normalize_company_url(normalized.company_url or normalized.website or normalized.company)
        normalized.metadata = dict(normalized.metadata or {})
        normalized.country = self._normalize_country(normalized.country)
        email_result = self._select_verified_email(
            company_url=normalized.company_url,
            primary_email=normalized.verified_email or normalized.email,
            metadata=normalized.metadata,
        )
        normalized.verified_email = email_result["verified_email"]
        normalized.service_reason = self._normalize_service_reason(normalized.service_reason)
        normalized.outreach_status = self._resolve_outreach_status(normalized.status, normalized.outreach_status)
        normalized.reply_status = self._normalize_reply_status(normalized.reply_status, normalized.metadata)
        normalized.followup_count = self._normalize_followup_count(normalized.followup_count, normalized.metadata)
        normalized.last_reply_at = self._normalize_last_reply_at(normalized.last_reply_at, normalized.metadata)
        normalized.email = normalized.verified_email
        normalized.website = normalized.company_url or normalized.website
        normalized.service_reason = self._safe_lead_reason(normalized)
        normalized.reason = normalized.service_reason or normalized.reason
        normalized.status = normalized.outreach_status or normalized.status
        normalized.industry = self._normalize_industry(normalized.industry)
        normalized.metadata["email_status"] = self._classify_email(normalized.email or normalized.verified_email)
        normalized.metadata["email_quality"] = email_result["email_quality"]
        normalized.metadata["email_confidence"] = email_result["email_confidence"]
        normalized.metadata["likely_email"] = email_result["likely_email"]
        original_phone = normalized.phone or self.phone_from_metadata(normalized.metadata)
        normalized_phone = self.normalize_phone(original_phone)
        if original_phone and normalized_phone and original_phone != normalized_phone:
            normalized.metadata["original_phone"] = original_phone
        normalized.phone = normalized_phone
        normalized.metadata["phone"] = normalized_phone
        normalized.metadata["whatsapp_ready"] = self.whatsapp_ready(normalized_phone)
        normalized.metadata.setdefault("whatsapp_status", "not_contacted")
        normalized.metadata["lead_readiness_score"] = self.lead_readiness_score(
            verified_email=normalized.verified_email,
            likely_email=email_result["likely_email"],
            phone=normalized.phone,
        )
        normalized.metadata["rejected_emails"] = email_result["rejected_emails"]
        return normalized

    async def generate_whatsapp_message_preview(self, tenant: TenantContext, lead_id: str, config: dict[str, Any] | None = None) -> dict[str, Any]:
        lead = await self.lead_for_tenant(tenant, lead_id)
        metadata = dict(lead.metadata or {})
        phone = self.normalize_phone(lead.phone or self.phone_from_metadata(metadata))
        message = self._whatsapp_message(lead, dict(config or {}))
        whatsapp_ready = self.whatsapp_ready(phone)
        return {
            "lead_id": lead.id,
            "whatsapp_ready": whatsapp_ready,
            "phone": phone,
            "whatsapp_message": message,
            "whatsapp_url": self.whatsapp_url(phone, message) if whatsapp_ready else "",
            "whatsapp_status": str(metadata.get("whatsapp_status", "not_contacted") or "not_contacted"),
            "lead_reason": str(lead.service_reason or "").strip(),
        }

    async def update_whatsapp_status(self, tenant: TenantContext, lead_id: str, status: str, message: str = "") -> Lead:
        allowed = {"not_contacted", "opened", "contacted", "replied", "interested", "not_interested"}
        normalized_status = str(status or "").strip().lower()
        if normalized_status not in allowed:
            raise ValueError("Invalid whatsapp_status.")
        scoped = self.db.for_tenant(tenant)
        lead = await self.lead_for_tenant(tenant, lead_id)
        metadata = dict(lead.metadata or {})
        metadata["whatsapp_status"] = normalized_status
        if message:
            metadata["whatsapp_message"] = str(message or "").strip()[:1000]
        if normalized_status in {"opened", "contacted"}:
            metadata["whatsapp_last_contacted_at"] = datetime.now(timezone.utc).isoformat()
        lead.metadata = metadata
        return await maybe_await(scoped.save("leads", lead))

    def _whatsapp_message(self, lead: Lead, config: dict[str, Any]) -> str:
        sender_name = str(config.get("sender_name", "") or "").strip()
        brand_name = str(config.get("brand_name", "") or "").strip()
        services = self._single_whatsapp_service(str(config.get("services_offered", "") or "").strip())
        cta = str(config.get("cta", "") or "").strip() or "Would you be open to a quick 10-minute chat?"
        if not cta.endswith("?"):
            cta = f"{cta}?"
        company = str(lead.company or lead.company_name or self._domain_label(lead.company_url or lead.website) or "your team").strip()
        intro = f"Hi {company} team"
        if sender_name and brand_name:
            intro = f"{intro}, this is {sender_name} from {brand_name}."
        elif sender_name:
            intro = f"{intro}, this is {sender_name}."
        else:
            intro = f"{intro}, hope you're doing well."
        reason = str(lead.service_reason or "").strip() or "This appears to be a relevant business with public contact details."
        help_line = f"I noticed {reason[:220]}"
        if services:
            help_line = f"{help_line} I thought {services} might be relevant for your team."
        else:
            help_line = f"{help_line} I thought a simple lead generation and follow-up system might be relevant for your team."
        message = f"{intro}\n\n{help_line}\n\n{cta}"
        return self._clean_forbidden_reason(message)[:1000]

    def _single_whatsapp_service(self, services: str) -> str:
        value = str(services or "").strip()
        if not value:
            return ""
        parts = [part.strip() for part in re.split(r",|;|\n", value) if part.strip()]
        if len(parts) > 3:
            return parts[0][:120]
        return value[:160]

    @classmethod
    def whatsapp_url(cls, phone: str, message: str) -> str:
        normalized = cls.normalize_phone(phone)
        if not cls.whatsapp_ready(normalized):
            return ""
        base_url = f"https://wa.me/{normalized.lstrip('+')}"
        text = str(message or "").strip()
        if not text:
            return base_url
        return f"{base_url}?text={quote(text)}"

    @classmethod
    def normalize_phone(cls, phone: str) -> str:
        raw = str(phone or "").strip()
        if not raw:
            return ""
        candidate = re.sub(r"[^\d+]", "", raw)
        if candidate.startswith("00"):
            candidate = f"+{candidate[2:]}"
        if candidate.count("+") > 1:
            return ""
        if "+" in candidate and not candidate.startswith("+"):
            return ""
        digits = re.sub(r"\D", "", candidate)
        if not 8 <= len(digits) <= 15:
            return ""
        return f"+{digits}" if candidate.startswith("+") else digits

    @classmethod
    def whatsapp_ready(cls, phone: str) -> bool:
        normalized = cls.normalize_phone(phone)
        if not normalized:
            return False
        digits = re.sub(r"\D", "", normalized)
        return 8 <= len(digits) <= 15 and len(set(digits)) > 2

    def _normalize_company_url(self, value: str) -> str:
        candidate = str(value or "").strip()
        if not candidate:
            return ""
        parsed = urlparse(candidate if "://" in candidate else f"https://{candidate}")
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return ""
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"

    def _normalize_country(self, value: str) -> str:
        candidate = str(value or "").strip()
        if not candidate:
            return ""
        lowered = candidate.lower()
        for alias, canonical in self.COUNTRY_ALIASES.items():
            if re.search(rf"\b{re.escape(alias)}\b", lowered):
                return canonical
        return ""

    def _normalize_industry(self, value: str) -> str:
        candidate = str(value or "").strip()
        if not candidate or candidate.lower() in {"unknown", "n/a", "na", "none"}:
            return "Other"
        lowered = candidate.lower()
        for keyword, canonical in self.INDUSTRY_MAP.items():
            if keyword in lowered:
                return canonical
        return re.sub(r"\s+", " ", candidate)[:120]

    def _normalize_email(self, value: str) -> str:
        candidate = str(value or "").strip().lower()
        if not candidate:
            return ""
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", candidate):
            return ""
        return candidate

    def _candidate_emails(self, primary_email: str, metadata: dict[str, Any]) -> list[str]:
        candidates: list[str] = []
        for value in (primary_email, metadata.get("email", ""), metadata.get("verified_email", "")):
            normalized = self._normalize_email(str(value or ""))
            if normalized and normalized not in candidates:
                candidates.append(normalized)
        for key in ("emails", "candidate_emails", "extracted_emails"):
            raw_values = metadata.get(key, [])
            if isinstance(raw_values, str):
                raw_values = [raw_values]
            if not isinstance(raw_values, list):
                continue
            for value in raw_values:
                normalized = self._normalize_email(str(value or ""))
                if normalized and normalized not in candidates:
                    candidates.append(normalized)
        raw_candidates = metadata.get("email_candidates", [])
        if isinstance(raw_candidates, list):
            for item in raw_candidates:
                if isinstance(item, dict):
                    value = item.get("email", "")
                else:
                    value = item
                normalized = self._normalize_email(str(value or ""))
                if normalized and normalized not in candidates:
                    candidates.append(normalized)
        return candidates

    def _likely_email(self, metadata: dict[str, Any]) -> str:
        for value in (metadata.get("likely_email", ""), metadata.get("likely_verified_email", "")):
            normalized = self._normalize_email(str(value or ""))
            if normalized:
                return normalized
        raw_values = metadata.get("likely_emails", [])
        if isinstance(raw_values, str):
            raw_values = [raw_values]
        if isinstance(raw_values, list):
            for value in raw_values:
                normalized = self._normalize_email(str(value or ""))
                if normalized:
                    return normalized
        return ""

    def _select_verified_email(self, company_url: str, primary_email: str, metadata: dict[str, Any]) -> dict[str, Any]:
        company_domain = self._domain_from_url(company_url)
        company_root = self._root_domain(company_domain)
        rejected: list[dict[str, str]] = []
        accepted: list[tuple[int, str, str, str]] = []
        for email in self._candidate_emails(primary_email, metadata):
            email_domain = email.split("@", 1)[-1]
            email_root = self._root_domain(email_domain)
            reason = self._email_rejection_reason(email_domain, email_root, company_root)
            if reason:
                rejected.append({"email": email, "reason": reason})
                continue
            quality = "generic" if self._is_generic_email(email) else "direct"
            confidence = "low" if quality == "generic" else "high"
            rank = 0 if quality == "direct" else 1
            accepted.append((rank, email, quality, confidence))

        if not accepted:
            likely_email = self._likely_email(metadata)
            return {
                "verified_email": "",
                "likely_email": likely_email,
                "email_quality": "likely" if likely_email else "missing",
                "email_confidence": "likely_email" if likely_email else "unknown",
                "rejected_emails": rejected,
            }
        accepted.sort(key=lambda item: item[0])
        _, email, quality, confidence = accepted[0]
        for _, rejected_email, _, _ in accepted[1:]:
            rejected.append({"email": rejected_email, "reason": "lower_ranked_same_domain_candidate"})
        return {
            "verified_email": email,
            "likely_email": "",
            "email_quality": quality,
            "email_confidence": "verified_email",
            "rejected_emails": rejected,
        }

    def _email_rejection_reason(self, email_domain: str, email_root: str, company_root: str) -> str:
        if not email_domain or not email_root:
            return "invalid_email_domain"
        if email_domain in self.PUBLIC_EMAIL_DOMAINS or email_root in self.PUBLIC_EMAIL_DOMAINS:
            return "public_email_domain"
        if self._is_suspicious_email_domain(email_domain):
            return "suspicious_email_domain"
        if not company_root:
            return "missing_company_domain"
        if email_root != company_root:
            return "domain_mismatch"
        return ""

    def _domain_from_url(self, value: str) -> str:
        candidate = str(value or "").strip()
        if not candidate:
            return ""
        parsed = urlparse(candidate if "://" in candidate else f"https://{candidate}")
        domain = parsed.netloc.lower().strip()
        return domain[4:] if domain.startswith("www.") else domain

    def _root_domain(self, domain: str) -> str:
        cleaned = str(domain or "").lower().strip().strip(".")
        if cleaned.startswith("www."):
            cleaned = cleaned[4:]
        parts = [part for part in cleaned.split(".") if part]
        if len(parts) < 2:
            return ""
        compound_suffixes = {
            "com.au", "com.sa", "co.uk", "com.pk", "com.br", "com.tr", "com.sg", "co.in", "co.za", "com.my"
        }
        suffix = ".".join(parts[-2:])
        if suffix in compound_suffixes and len(parts) >= 3:
            return ".".join(parts[-3:])
        return ".".join(parts[-2:])

    def _is_suspicious_email_domain(self, domain: str) -> bool:
        parts = [part for part in str(domain or "").lower().split(".") if part]
        if len(parts) < 2:
            return True
        root_label = parts[-2]
        tld = parts[-1]
        return len(root_label) < 2 or len(tld) < 2 or not all(part.isalnum() or "-" in part for part in parts)

    def _is_generic_email(self, email: str) -> bool:
        return any(str(email or "").lower().startswith(prefix) for prefix in self.GENERIC_EMAIL_PREFIXES)

    def _classify_email(self, value: str) -> str:
        candidate = str(value or "").strip().lower()
        if not candidate:
            return "missing"
        if any(candidate.startswith(prefix) for prefix in self.GENERIC_EMAIL_PREFIXES):
            return "generic"
        if "@" not in candidate or "." not in candidate.split("@")[-1]:
            return "invalid"
        return "verified"

    def _normalize_service_reason(self, value: str) -> str:
        candidate = re.sub(r"\s+", " ", str(value or "").strip())
        if not candidate:
            return ""
        if self._looks_like_score_breakdown(candidate):
            return ""
        candidate = re.sub(r"^(reason|service_reason|why):\s*", "", candidate, flags=re.IGNORECASE)
        candidate = re.sub(r"\s*\|\s*(fallback_reason|quality_filter)=.*$", "", candidate, flags=re.IGNORECASE)
        candidate = candidate.strip(" -;")
        return self._clean_forbidden_reason(candidate)[:220]

    def _safe_lead_reason(self, lead: Lead) -> str:
        metadata = dict(lead.metadata or {})
        for value in (
            lead.service_reason,
            lead.reason,
            metadata.get("lead_reason", ""),
            metadata.get("AIReason", ""),
            metadata.get("Reason", ""),
            metadata.get("intent_summary", ""),
            metadata.get("company_summary", ""),
        ):
            cleaned = self._normalize_service_reason(str(value or ""))
            if cleaned:
                return cleaned[:280]
        if lead.industry:
            return f"This company appears to work in {lead.industry.strip()}, making it relevant for business outreach."
        return "This appears to be a relevant business with public contact details."

    def _clean_forbidden_reason(self, value: str) -> str:
        text = str(value or "").strip()
        replacements = {
            "matches the technology target from the search query": "appears to offer technology services",
            "target from the search query": "business category",
            "matches the search query": "appears to be relevant",
            "search query": "business category",
            "scraped data": "public information",
            "AI detected": "it appears",
            "AI marked": "it appears",
            "target matched": "appears relevant",
            "target matching": "relevance",
        }
        for bad, good in replacements.items():
            text = re.sub(re.escape(bad), good, text, flags=re.IGNORECASE)
        text = re.sub(r"\s+", " ", text).strip()
        sentences = re.split(r"(?<=[.!?])\s+", text)
        return " ".join(sentences[:2]).strip()

    def _domain_label(self, url: str) -> str:
        return self._domain_from_url(url).replace("www.", "")

    def _looks_like_score_breakdown(self, value: str) -> bool:
        lowered = str(value or "").lower()
        return any(
            marker in lowered
            for marker in (
                "email=40/40",
                "phone=25/25",
                "relevance=",
                "quality=",
                "minimum_fallback=",
                "missing email -> marked no_email",
            )
        )

    def _normalize_outreach_status(self, value: str) -> str:
        candidate = str(value or "").strip().lower()
        return candidate if candidate in {"pending", "sent", "failed", "blocked"} else "pending"

    def _resolve_outreach_status(self, status: str, outreach_status: str) -> str:
        normalized_status = self._normalize_outreach_status(status)
        normalized_outreach = self._normalize_outreach_status(outreach_status)
        if normalized_status in {"sent", "failed", "blocked"} and normalized_outreach == "pending":
            return normalized_status
        return normalized_outreach or normalized_status

    def _normalize_reply_status(self, value: str, metadata: dict[str, Any]) -> str:
        candidate = str(value or metadata.get("ReplyStatus", "")).strip().lower()
        mapping = {
            "received": "replied",
            "no reply": "no_reply",
            "interested": "interested",
            "not interested": "not_interested",
            "not_interested": "not_interested",
            "replied": "replied",
            "replied_positive": "replied_positive",
            "replied_negative": "replied_negative",
        }
        return mapping.get(candidate, "no_reply")

    def _normalize_followup_count(self, value: int, metadata: dict[str, Any]) -> int:
        raw_value = value if value is not None else metadata.get("FollowupCount", 0)
        try:
            return max(0, int(raw_value or 0))
        except (TypeError, ValueError):
            return 0

    def _normalize_last_reply_at(self, value: datetime | None, metadata: dict[str, Any]) -> datetime | None:
        if value:
            return value
        raw = str(metadata.get("LastReplyAt", "")).strip()
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None

    @classmethod
    def phone_from_metadata(cls, metadata: dict[str, Any]) -> str:
        for key in ("phone", "Phone", "phone_number", "contact_phone", "mobile", "whatsapp", "whatsapp_number"):
            value = str(metadata.get(key, "") or "").strip()
            if value:
                return value
        contact = metadata.get("contact", {}) if isinstance(metadata.get("contact", {}), dict) else {}
        for key in ("phone", "mobile", "whatsapp", "whatsapp_number"):
            value = str(contact.get(key, "") or "").strip()
            if value:
                return value
        return ""

    @classmethod
    def likely_email_from_metadata(cls, metadata: dict[str, Any]) -> str:
        value = str(metadata.get("likely_email", "") or "").strip().lower()
        if value:
            return value
        raw_values = metadata.get("likely_emails", [])
        if isinstance(raw_values, str):
            raw_values = [raw_values]
        if isinstance(raw_values, list):
            for raw_value in raw_values:
                value = str(raw_value or "").strip().lower()
                if value:
                    return value
        return ""

    @classmethod
    def lead_readiness_score(cls, verified_email: str, likely_email: str, phone: str) -> int:
        if str(verified_email or "").strip():
            return 100
        if str(likely_email or "").strip():
            return 70
        if str(phone or "").strip():
            return 40
        return 0

    async def dashboard_snapshot(self, tenant: TenantContext) -> dict[str, int | str]:
        scoped = self.db.for_tenant(tenant)
        leads = await maybe_await(scoped.list("leads"))
        jobs = await maybe_await(scoped.list("jobs"))
        replies = await maybe_await(scoped.list("replies"))
        total_leads = len(leads)
        leads_with_website = 0
        leads_with_phone = 0
        leads_with_email = 0
        leads_with_verified_email = 0
        likely_email_leads = 0
        phone_only_leads = 0
        no_contact_leads = 0
        for lead in leads:
            metadata = dict(lead.metadata or {})
            likely_email = self.likely_email_from_metadata(metadata)
            phone = str(lead.phone or "").strip() or self.phone_from_metadata(metadata)
            has_verified_email = bool(str(lead.verified_email or "").strip())
            has_any_email = has_verified_email or bool(str(lead.email or "").strip()) or bool(metadata.get("email")) or bool(metadata.get("candidate_emails")) or bool(metadata.get("email_candidates"))
            if str(lead.company_url or lead.website or "").strip():
                leads_with_website += 1
            if phone:
                leads_with_phone += 1
            if has_any_email:
                leads_with_email += 1
            if has_verified_email:
                leads_with_verified_email += 1
            elif likely_email:
                likely_email_leads += 1
            elif phone:
                phone_only_leads += 1
            else:
                no_contact_leads += 1
        verified_email_rate = round((leads_with_verified_email / total_leads) * 100, 1) if total_leads else 0.0
        return {
            "tenant_id": tenant.tenant_id,
            "lead_count": total_leads,
            "sent_count": len([lead for lead in leads if str(lead.status or "").strip().lower() == "sent"]),
            "reply_count": len(replies),
            "bounced_count": len([lead for lead in leads if str((lead.metadata or {}).get("BounceStatus", "")).strip().lower() == "bounced"]),
            "scheduled_followup_count": len([lead for lead in leads if str((lead.metadata or {}).get("NextFollowupDue", "") or (lead.metadata or {}).get("ScheduledFollowupAt", "")).strip()]),
            "job_count": len(jobs),
            "leads_with_website": leads_with_website,
            "leads_with_phone": leads_with_phone,
            "leads_with_email": leads_with_email,
            "leads_with_verified_email": leads_with_verified_email,
            "verified_email_rate": verified_email_rate,
            "email_ready_leads": leads_with_verified_email,
            "likely_email_leads": likely_email_leads,
            "phone_only_leads": phone_only_leads,
            "no_contact_leads": no_contact_leads,
        }
