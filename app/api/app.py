"""Simple API facade and FastAPI app for the multi-tenant SaaS runtime."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import logging
import os
from pathlib import Path
from typing import Any, Dict, Sequence
from urllib.parse import urlencode

import httpx
from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from app.configs.settings import settings
from app.middleware.auth import AuthTenantMiddleware
from app.middleware.security import InMemoryRateLimitMiddleware, SecurityHeadersMiddleware
from app.core.auth import create_jwt_token, decode_jwt_token, get_plan_limits, is_plan_gated_agent, validate_production_jwt_secret
from app.core.models import Job, Lead, TenantContext
from app.core.tenant import get_current_tenant, resolve_tenant_context
from app.db.postgres import initialize_async_database, verify_async_database
from app.db.session import AsyncDatabaseSession, DatabaseSession, get_async_db_session, reset_async_session_factory
from app.services.auth_service import AuthService
from app.services.admin_analytics_service import AdminAnalyticsService
from app.services.agency_growth_service import AgencyGrowthService
from app.services.agency_kit_service import AgencyKitLimitError, AgencyKitService
from app.services.ai_provider_service import AIProviderNotConfigured, AIProviderService
from app.services.billing_service import BillingService
from app.services.job_service import JobService
from app.services.lead_service import LeadService
from app.services.marketing_campaign_service import MarketingCampaignLimitError, MarketingCampaignService
from app.services.plan_gate import PlanGateError, require_pro_plan
from app.services.provider_credential_service import ProviderCredentialService
from app.services.security_service import SecretEncryptionError
from app.services._async import maybe_await

LOGGER = logging.getLogger(__name__)

GMAIL_OAUTH_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]
GOOGLE_OAUTH_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_GMAIL_PROFILE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"


@dataclass(slots=True)
class ApiApplication:
    db: DatabaseSession

    def tenant_context(self, tenant_id: str = "", tenant_slug: str = "", user_id: str = "") -> TenantContext:
        if tenant_id:
            return resolve_tenant_context(tenant_id=tenant_id, tenant_slug=tenant_slug, user_id=user_id)
        return get_current_tenant()

    async def list_leads(self, tenant_id: str = "", tenant_slug: str = "", user_id: str = "") -> Sequence[Lead]:
        tenant = self.tenant_context(tenant_id=tenant_id, tenant_slug=tenant_slug, user_id=user_id)
        return await LeadService(self.db).list_leads(tenant)

    async def enqueue_agent_job(
        self,
        tenant_id: str = "",
        agent_name: str = "",
        payload: Dict[str, Any] | None = None,
        tenant_slug: str = "",
        user_id: str = "",
    ) -> Dict[str, str]:
        tenant = self.tenant_context(tenant_id=tenant_id, tenant_slug=tenant_slug, user_id=user_id)
        job = await JobService(self.db).enqueue(tenant, name=agent_name, payload=dict(payload or {}))
        return {"job_id": job.id, "tenant_id": tenant.tenant_id, "agent_name": agent_name}

    async def signup(
        self,
        tenant_id: str,
        tenant_name: str,
        tenant_slug: str,
        email: str,
        password: str,
        full_name: str,
        plan: str = "Starter",
    ) -> Dict[str, Any]:
        result = await AuthService(self.db).signup(
            tenant_id=tenant_id,
            tenant_name=tenant_name,
            tenant_slug=tenant_slug,
            email=email,
            password=password,
            full_name=full_name,
            plan=plan,
        )
        return asdict(result)

    async def login(self, tenant_id: str, email: str, password: str) -> Dict[str, Any]:
        result = await AuthService(self.db).login(tenant_id=tenant_id, email=email, password=password)
        return asdict(result)

    async def authenticate(self, token: str) -> Dict[str, Any]:
        return await AuthService(self.db).authenticate_token(token)

    def plan_limits(self, plan: str) -> Dict[str, int]:
        return get_plan_limits(plan)

    async def consume_usage(self, tenant_id: str, metric: str, amount: int = 1) -> Dict[str, int]:
        tenant = await maybe_await(self.db.tenants.list(tenant_id))
        if not tenant:
            raise ValueError("Tenant does not exist.")
        return await AuthService(self.db).enforce_usage_limit(tenant[0], metric=metric, amount=amount)


class SignupRequest(BaseModel):
    tenant_id: str
    tenant_name: str
    tenant_slug: str
    email: str
    password: str
    full_name: str
    plan: str = "Starter"


class LoginRequest(BaseModel):
    tenant_id: str
    email: str
    password: str


class GmailProviderSettingsRequest(BaseModel):
    client_id: str
    client_secret: str
    refresh_token: str
    sender_email: str


class AIProviderSettingsRequest(BaseModel):
    provider: str = "fallback"
    api_key: str = ""
    model: str = ""
    enabled: bool = True


class LeadUpsertRequest(BaseModel):
    company_url: str = ""
    country: str = ""
    verified_email: str = ""
    service_reason: str = ""
    outreach_status: str = "pending"
    followup_count: int = 0
    reply_status: str = "no_reply"
    last_reply_at: str = ""
    company: str = ""
    website: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    industry: str = ""
    score: int = 0
    reason: str = ""
    status: str = "pending"
    source_query: str = ""
    metadata: Dict[str, Any] = Field(default_factory=dict)


class JobRequest(BaseModel):
    agent_name: str
    payload: Dict[str, Any] = Field(default_factory=dict)


class AgencyKitBulkRequest(BaseModel):
    lead_ids: list[str] = Field(default_factory=list)


class MarketingCampaignIdeaRequest(BaseModel):
    business_idea: str
    target_location: str = ""
    target_country: str = ""
    target_city: str = ""
    target_audience: str = ""
    campaign_goal: str = ""


class MiniAgencyPlanRequest(BaseModel):
    skill: str = "other"
    target_country: str = ""
    target_city: str = ""
    daily_time: str = "1 hour"
    goal: str = "first client"
    preferred_niche: str = ""


class RunOnceRequest(BaseModel):
    job_type: str = ""


class SubscribeRequest(BaseModel):
    plan: str


class ApprovePaymentRequest(BaseModel):
    payment_reference_id: str


def _serialize_lead(lead: Lead, include_agency_kit: bool = False) -> Dict[str, Any]:
    last_reply_at = lead.last_reply_at
    metadata = dict(lead.metadata or {})
    payload = {
        "company_url": str(lead.company_url or "").strip(),
        "country": str(lead.country or "").strip(),
        "verified_email": str(lead.verified_email or "").strip().lower(),
        "service_reason": str(lead.service_reason or "").strip(),
        "industry": str(lead.industry or "").strip(),
        "score": int(lead.score or 0),
        "outreach_status": str(lead.outreach_status or "").strip().lower(),
        "followup_count": int(lead.followup_count or 0),
        "reply_status": str(lead.reply_status or "").strip().lower(),
        "last_reply_at": last_reply_at.isoformat() if hasattr(last_reply_at, "isoformat") else str(last_reply_at or ""),
    }
    if include_agency_kit:
        payload["id"] = lead.id
        payload["agency_kit"] = metadata.get("agency_kit", {})
        payload["offer_match"] = metadata.get("offer_match", {})
        payload["whatsapp_sales_kit"] = metadata.get("whatsapp_sales_kit", {})
        payload["marketing_campaign_kit"] = metadata.get("marketing_campaign_kit", {})
    return payload


def _serialize_job(job: Job) -> Dict[str, Any]:
    return {
        "job_id": job.id,
        "tenant_id": job.tenant_id,
        "job_type": str(job.name or job.job_type or "").strip(),
        "status": str(job.status or "").strip().lower(),
        "created_at": job.created_at.isoformat() if hasattr(job.created_at, "isoformat") else str(job.created_at or ""),
        "updated_at": job.updated_at.isoformat() if hasattr(job.updated_at, "isoformat") else str(job.updated_at or ""),
        "error": str(job.error or "").strip(),
    }


def _is_debug_endpoint_enabled() -> bool:
    environment = str(os.getenv("APP_ENV") or os.getenv("ENV") or settings.environment or "development").strip().lower()
    debug_enabled = str(os.getenv("DEBUG", "")).strip().lower() in {"1", "true", "yes", "on"}
    return environment == "development" or debug_enabled


def _settings_value(attribute: str, env_name: str, default: str = "") -> str:
    return str(getattr(settings, attribute, "") or os.getenv(env_name, default) or "").strip()


def _google_oauth_client_id() -> str:
    return _settings_value("google_oauth_client_id", "GOOGLE_OAUTH_CLIENT_ID")


def _google_oauth_client_secret() -> str:
    return _settings_value("google_oauth_client_secret", "GOOGLE_OAUTH_CLIENT_SECRET")


def _google_oauth_redirect_uri() -> str:
    return _settings_value("google_oauth_redirect_uri", "GOOGLE_OAUTH_REDIRECT_URI")


def _gmail_frontend_redirect_url(success: bool, message: str) -> str:
    status = "success" if success else "error"
    base_url = _settings_value("frontend_base_url", "APP_FRONTEND_URL").rstrip("/")
    params = urlencode({"settings": "gmail", "gmail_oauth": status, "message": message})
    if base_url:
        return f"{base_url}/?{params}"
    return f"/?{params}"


def _gmail_redirect_response(success: bool, message: str) -> RedirectResponse:
    return RedirectResponse(_gmail_frontend_redirect_url(success, message), status_code=302)


def _gmail_oauth_redirect_uri(request: Request) -> str:
    configured = _google_oauth_redirect_uri()
    if configured:
        return configured
    return str(request.url_for("gmail_oauth_callback"))


def _build_gmail_authorization_url(redirect_uri: str, state: str) -> str:
    params = {
        "client_id": _google_oauth_client_id(),
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(GMAIL_OAUTH_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"{GOOGLE_OAUTH_AUTH_URL}?{urlencode(params)}"


async def exchange_gmail_oauth_code(code: str, redirect_uri: str) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            GOOGLE_OAUTH_TOKEN_URL,
            data={
                "code": code,
                "client_id": _google_oauth_client_id(),
                "client_secret": _google_oauth_client_secret(),
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response.raise_for_status()
        data = response.json()
    return dict(data)


async def fetch_gmail_profile_email(access_token: str) -> str:
    if not access_token:
        return ""
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(GOOGLE_GMAIL_PROFILE_URL, headers={"Authorization": f"Bearer {access_token}"})
        if response.status_code >= 400:
            return ""
        data = response.json()
    return str(data.get("emailAddress", "") or "").strip().lower()


def create_fastapi_app(db: DatabaseSession | None = None) -> FastAPI:
    validate_production_jwt_secret()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if shared_db is None and settings.database_backend == "postgres":
            await initialize_async_database()
        yield
        if shared_db is None and settings.database_backend == "postgres":
            await reset_async_session_factory()

    app = FastAPI(title="Lead Generator SaaS API", lifespan=lifespan)
    from app.workers.runner import build_job_queue
    shared_db = db
    if shared_db is not None:
        api = ApiApplication(shared_db)
        app.state.db = shared_db
        app.state.api = api
    app.state.queue = build_job_queue(shared_db)
    app.add_middleware(AuthTenantMiddleware)
    app.add_middleware(InMemoryRateLimitMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/healthz")
    async def healthz() -> Dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> Dict[str, Any]:
        if settings.database_backend != "postgres":
            raise HTTPException(status_code=503, detail="Production runtime requires DATABASE_BACKEND=postgres.")
        db_status = await verify_async_database()
        return {
            **db_status,
            "queue_ready": bool(getattr(app.state, "queue", None) is not None),
        }

    async def get_db_dependency():
        if shared_db is not None:
            yield shared_db
            return
        async with get_async_db_session() as live_db:
            yield live_db

    def require_admin(request: Request) -> None:
        role = str(getattr(request.state, "user", {}).get("role", "")).strip().lower()
        if role != "admin":
            raise HTTPException(status_code=403, detail="Admin access required.")

    async def require_pro_features(tenant: TenantContext, db_session: DatabaseSession | AsyncDatabaseSession, message: str) -> None:
        try:
            await require_pro_plan(db_session, tenant, message)
        except PlanGateError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error

    @app.post("/signup")
    async def signup(
        payload: SignupRequest,
        db_session: DatabaseSession | AsyncDatabaseSession = Depends(get_db_dependency),
    ) -> Dict[str, Any]:
        try:
            result = await AuthService(db_session).signup(**payload.model_dump())
            return asdict(result)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/login")
    async def login(
        payload: LoginRequest,
        db_session: DatabaseSession | AsyncDatabaseSession = Depends(get_db_dependency),
    ) -> Dict[str, Any]:
        try:
            result = await AuthService(db_session).login(**payload.model_dump())
            return asdict(result)
        except ValueError as error:
            raise HTTPException(status_code=401, detail=str(error)) from error

    @app.get("/leads")
    async def list_leads(
        request: Request,
        include_agency_kit: bool = False,
        db_session: DatabaseSession | AsyncDatabaseSession = Depends(get_db_dependency),
    ) -> Dict[str, Any]:
        tenant = request.state.tenant
        items = await LeadService(db_session).list_leads(tenant)
        return {
            "tenant_id": tenant.tenant_id,
            "items": [_serialize_lead(item, include_agency_kit=include_agency_kit) for item in items],
        }

    @app.get("/debug/leads/serialization")
    async def debug_lead_serialization(
        request: Request,
        db_session: DatabaseSession | AsyncDatabaseSession = Depends(get_db_dependency),
    ) -> Dict[str, Any]:
        if not _is_debug_endpoint_enabled():
            raise HTTPException(status_code=404, detail="Not found")
        tenant = request.state.tenant
        items = await LeadService(db_session).list_leads(tenant)
        serialized = _serialize_lead(items[0]) if items else {}
        return {
            "tenant_id": tenant.tenant_id,
            "item_count": len(items),
            "sample": serialized,
        }

    @app.post("/leads")
    async def upsert_lead(
        payload: LeadUpsertRequest,
        request: Request,
        db_session: DatabaseSession | AsyncDatabaseSession = Depends(get_db_dependency),
    ) -> Dict[str, Any]:
        tenant = request.state.tenant
        data = payload.model_dump()
        lead = Lead(
            tenant_id=tenant.tenant_id,
            company_url=str(data.get("company_url") or data.get("website") or "").strip(),
            country=str(data.get("country") or "").strip(),
            verified_email=str(data.get("verified_email") or data.get("email") or "").strip(),
            service_reason=str(data.get("service_reason") or "").strip(),
            outreach_status=str(data.get("outreach_status") or data.get("status") or "pending").strip(),
            followup_count=int(data.get("followup_count") or data.get("metadata", {}).get("FollowupCount", 0) or 0),
            reply_status=str(data.get("reply_status") or data.get("metadata", {}).get("ReplyStatus", "no_reply")).strip(),
            last_reply_at=None,
            company=str(data.get("company") or "").strip(),
            website=str(data.get("website") or data.get("company_url") or "").strip(),
            email=str(data.get("email") or data.get("verified_email") or "").strip(),
            phone=str(data.get("phone") or "").strip(),
            location=str(data.get("location") or data.get("country") or "").strip(),
            industry=str(data.get("industry") or "").strip(),
            score=int(data.get("score") or 0),
            reason=str(data.get("reason") or data.get("service_reason") or "").strip(),
            status=str(data.get("status") or data.get("outreach_status") or "pending").strip(),
            source_query=str(data.get("source_query") or "").strip(),
            metadata=dict(data.get("metadata") or {}),
        )
        saved = await LeadService(db_session).upsert_lead(tenant, lead)
        return asdict(saved)

    @app.post("/leads/{lead_id}/agency-kit")
    async def generate_agency_kit(
        lead_id: str,
        request: Request,
        db_session: DatabaseSession | AsyncDatabaseSession = Depends(get_db_dependency),
    ) -> Dict[str, Any]:
        tenant = request.state.tenant
        try:
            kit = await AgencyKitService(db_session).generate_for_lead(tenant, lead_id)
        except AgencyKitLimitError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return {"tenant_id": tenant.tenant_id, "lead_id": lead_id, "agency_kit": kit}

    @app.post("/leads/agency-kit/bulk")
    async def generate_agency_kit_bulk(
        payload: AgencyKitBulkRequest,
        request: Request,
        db_session: DatabaseSession | AsyncDatabaseSession = Depends(get_db_dependency),
    ) -> Dict[str, Any]:
        tenant = request.state.tenant
        try:
            return await AgencyKitService(db_session).generate_bulk(tenant, payload.lead_ids)
        except AgencyKitLimitError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error

    @app.post("/marketing/campaign/from-idea")
    async def generate_marketing_campaign_from_idea(
        payload: MarketingCampaignIdeaRequest,
        request: Request,
        db_session: DatabaseSession | AsyncDatabaseSession = Depends(get_db_dependency),
    ) -> Dict[str, Any]:
        tenant = request.state.tenant
        location = " ".join(
            item.strip()
            for item in [payload.target_location, payload.target_city, payload.target_country]
            if item.strip()
        ).strip()
        try:
            campaign = await MarketingCampaignService(db_session).generate_from_idea(
                tenant=tenant,
                business_idea=payload.business_idea,
                target_location=location,
                target_audience=payload.target_audience,
                campaign_goal=payload.campaign_goal,
            )
        except MarketingCampaignLimitError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        return {"tenant_id": tenant.tenant_id, "marketing_campaign_kit": campaign}

    @app.post("/marketing/campaign/from-lead/{lead_id}")
    async def generate_marketing_campaign_from_lead(
        lead_id: str,
        request: Request,
        db_session: DatabaseSession | AsyncDatabaseSession = Depends(get_db_dependency),
    ) -> Dict[str, Any]:
        tenant = request.state.tenant
        try:
            campaign = await MarketingCampaignService(db_session).generate_from_lead(tenant, lead_id)
        except MarketingCampaignLimitError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return {"tenant_id": tenant.tenant_id, "lead_id": lead_id, "marketing_campaign_kit": campaign}

    @app.post("/leads/{lead_id}/offer-match")
    async def generate_offer_match(
        lead_id: str,
        request: Request,
        db_session: DatabaseSession | AsyncDatabaseSession = Depends(get_db_dependency),
    ) -> Dict[str, Any]:
        tenant = request.state.tenant
        service = AgencyGrowthService(db_session)
        try:
            lead = await service.lead_for_tenant(tenant, lead_id)
            offer_match = await service.generate_offer_match(lead, tenant)
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return {"tenant_id": tenant.tenant_id, "lead_id": lead_id, "offer_match": offer_match}

    @app.post("/leads/{lead_id}/whatsapp-sales-kit")
    async def generate_whatsapp_sales_kit(
        lead_id: str,
        request: Request,
        db_session: DatabaseSession | AsyncDatabaseSession = Depends(get_db_dependency),
    ) -> Dict[str, Any]:
        tenant = request.state.tenant
        service = AgencyGrowthService(db_session)
        try:
            lead = await service.lead_for_tenant(tenant, lead_id)
            sales_kit = await service.generate_whatsapp_sales_kit(lead, tenant)
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return {"tenant_id": tenant.tenant_id, "lead_id": lead_id, "whatsapp_sales_kit": sales_kit}

    @app.post("/agency/mini-agency-plan")
    async def generate_mini_agency_plan(
        payload: MiniAgencyPlanRequest,
        request: Request,
        db_session: DatabaseSession | AsyncDatabaseSession = Depends(get_db_dependency),
    ) -> Dict[str, Any]:
        tenant = request.state.tenant
        plan = await AgencyGrowthService(db_session).generate_mini_agency_plan(payload.model_dump(), tenant)
        return {"tenant_id": tenant.tenant_id, "mini_agency_plan": plan}

    @app.post("/jobs")
    async def enqueue_job(
        payload: JobRequest,
        request: Request,
        db_session: DatabaseSession | AsyncDatabaseSession = Depends(get_db_dependency),
    ) -> Dict[str, Any]:
        tenant = request.state.tenant
        if is_plan_gated_agent(payload.agent_name):
            await require_pro_features(tenant, db_session, "Outreach is available in Pro plan.")
        job = await JobService(db_session).enqueue(tenant, name=payload.agent_name, payload=payload.payload)
        await app.state.queue.register(job.id)
        return {"job_id": job.id, "tenant_id": tenant.tenant_id, "agent_name": payload.agent_name}

    @app.get("/jobs/recent")
    async def recent_jobs(
        request: Request,
        db_session: DatabaseSession | AsyncDatabaseSession = Depends(get_db_dependency),
    ) -> Dict[str, Any]:
        tenant = request.state.tenant
        jobs = await maybe_await(db_session.jobs.list(tenant.tenant_id))
        recent = sorted(jobs, key=lambda item: item.updated_at, reverse=True)[:20]
        return {"tenant_id": tenant.tenant_id, "items": [_serialize_job(job) for job in recent]}

    @app.post("/jobs/run-once")
    async def run_job_once(request: Request, payload: RunOnceRequest | None = Body(default=None)) -> Dict[str, Any]:
        tenant = request.state.tenant
        try:
            result = await app.state.queue.run_once_for_tenant(tenant, job_type=(payload.job_type if payload else ""))
            if not isinstance(result, dict):
                return JSONResponse(
                    status_code=200,
                    content={"status": "FAILED", "message": "Invalid job result.", "data": {}},
                )
            queue_status = str(result.get("status", "failed")).strip().lower()
            agent_result = result.get("result", {})
            agent_status = ""
            if isinstance(agent_result, dict):
                agent_status = str(agent_result.get("status", "")).strip().upper()
            error_message = str(result.get("error", "")).strip()
            if error_message == "Tenant Gmail credentials are not configured.":
                error_message = "Gmail credentials are not configured. Please connect Gmail first."
            message = str(
                (agent_result.get("message", "") if isinstance(agent_result, dict) else "")
                or result.get("message", "")
                or error_message
                or ("Job completed." if queue_status == "completed" else "Job failed.")
            )
            data = None
            if isinstance(agent_result, dict):
                data = agent_result.get("data")
            if data is None:
                data = agent_result if isinstance(agent_result, dict) else result.get("result", {})
            return JSONResponse(
                status_code=200,
                content={
                    "status": queue_status,
                    "agent_status": agent_status,
                    "message": message,
                    "data": data if isinstance(data, dict) else {"value": data},
                },
            )
        except Exception as error:
            LOGGER.exception("run_once endpoint failed for tenant=%s", getattr(tenant, "tenant_id", ""))
            return JSONResponse(
                status_code=200,
                content={
                    "status": "FAILED",
                    "message": "Lead generation failed safely.",
                    "data": {},
                },
            )

    @app.get("/settings/providers/gmail/oauth/start")
    async def start_gmail_oauth(
        request: Request,
        db_session: DatabaseSession | AsyncDatabaseSession = Depends(get_db_dependency),
    ) -> Dict[str, Any]:
        tenant = request.state.tenant
        await require_pro_features(tenant, db_session, "Gmail automation is a Pro feature.")
        if not _google_oauth_client_id() or not _google_oauth_client_secret():
            raise HTTPException(status_code=400, detail="Google OAuth is not configured.")
        state = create_jwt_token(
            {
                "purpose": "gmail_oauth",
                "tenant_id": tenant.tenant_id,
                "tenant_slug": tenant.tenant_slug,
                "user_id": tenant.user_id,
                "email": str(tenant.metadata.get("email", "") if tenant.metadata else ""),
            },
            expires_in_seconds=600,
        )
        redirect_uri = _gmail_oauth_redirect_uri(request)
        return {"authorization_url": _build_gmail_authorization_url(redirect_uri, state)}

    @app.get("/settings/providers/gmail/oauth/callback")
    async def gmail_oauth_callback(
        request: Request,
        code: str = "",
        state: str = "",
        error: str = "",
        db_session: DatabaseSession | AsyncDatabaseSession = Depends(get_db_dependency),
    ) -> RedirectResponse:
        if error:
            return _gmail_redirect_response(False, "Google authorization was cancelled.")
        if not code or not state:
            return _gmail_redirect_response(False, "Google authorization response was incomplete.")
        try:
            state_payload = decode_jwt_token(state)
            if state_payload.get("purpose") != "gmail_oauth":
                raise ValueError("Invalid OAuth state.")
            tenant_id = str(state_payload.get("tenant_id", "") or "").strip()
            user_id = str(state_payload.get("user_id", "") or "").strip()
            if not tenant_id or not user_id:
                raise ValueError("Invalid OAuth state.")
        except ValueError:
            return _gmail_redirect_response(False, "Google authorization state was invalid or expired.")

        redirect_uri = _gmail_oauth_redirect_uri(request)
        try:
            token_data = await exchange_gmail_oauth_code(code, redirect_uri)
            refresh_token = str(token_data.get("refresh_token", "") or "").strip()
            access_token = str(token_data.get("access_token", "") or "").strip()
            if not refresh_token:
                return _gmail_redirect_response(False, "Google did not return offline access. Please reconnect Gmail.")
            email_address = await fetch_gmail_profile_email(access_token)
            if not email_address:
                email_address = str(state_payload.get("email", "") or "").strip().lower()
            expiry = ""
            expires_in = int(token_data.get("expires_in", 0) or 0)
            if expires_in:
                expiry = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()
            tenant = TenantContext(
                tenant_id=tenant_id,
                tenant_slug=str(state_payload.get("tenant_slug", "") or "").strip(),
                user_id=user_id,
                metadata={"email": str(state_payload.get("email", "") or "")},
            )
            await ProviderCredentialService(db_session).save_gmail_oauth_credentials(
                tenant,
                {
                    "client_id": _google_oauth_client_id(),
                    "client_secret": _google_oauth_client_secret(),
                    "refresh_token": refresh_token,
                    "access_token": access_token,
                    "expiry": expiry,
                    "email_address": email_address,
                    "token_uri": GOOGLE_OAUTH_TOKEN_URL,
                    "scopes": GMAIL_OAUTH_SCOPES,
                },
            )
        except SecretEncryptionError:
            return _gmail_redirect_response(False, "Secure Gmail storage is not configured.")
        except Exception:
            return _gmail_redirect_response(False, "Google authorization failed safely.")
        return _gmail_redirect_response(True, "Gmail connected successfully.")

    @app.post("/settings/providers/gmail")
    async def save_gmail_provider_settings(
        payload: GmailProviderSettingsRequest,
        request: Request,
        db_session: DatabaseSession | AsyncDatabaseSession = Depends(get_db_dependency),
    ) -> Dict[str, Any]:
        tenant = request.state.tenant
        await require_pro_features(tenant, db_session, "Gmail automation is a Pro feature.")
        credentials = {
            "client_id": payload.client_id.strip(),
            "client_secret": payload.client_secret.strip(),
            "refresh_token": payload.refresh_token.strip(),
            "email_address": payload.sender_email.strip().lower(),
            "scopes": GMAIL_OAUTH_SCOPES,
        }
        try:
            await ProviderCredentialService(db_session).save_gmail_credentials(tenant, credentials)
        except SecretEncryptionError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {
            "configured": True,
            "connected": True,
            "sender_email": credentials["email_address"],
        }

    @app.get("/settings/providers/gmail/status")
    async def gmail_provider_status(
        request: Request,
        db_session: DatabaseSession | AsyncDatabaseSession = Depends(get_db_dependency),
    ) -> Dict[str, Any]:
        tenant = request.state.tenant
        await require_pro_features(tenant, db_session, "Gmail automation is a Pro feature.")
        try:
            credentials = await ProviderCredentialService(db_session).get_gmail_credentials(tenant)
        except SecretEncryptionError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        sender_email = str(credentials.get("email_address", "")).strip().lower() if credentials else ""
        connected = bool(credentials.get("refresh_token") or credentials.get("access_token")) if credentials else False
        return {
            "configured": bool(credentials) and connected,
            "connected": connected,
            "sender_email": sender_email,
        }

    @app.post("/settings/providers/gmail/disconnect")
    async def disconnect_gmail_provider(
        request: Request,
        db_session: DatabaseSession | AsyncDatabaseSession = Depends(get_db_dependency),
    ) -> Dict[str, Any]:
        tenant = request.state.tenant
        await require_pro_features(tenant, db_session, "Gmail automation is a Pro feature.")
        await ProviderCredentialService(db_session).disconnect_gmail(tenant)
        return {"configured": False, "connected": False, "sender_email": ""}

    @app.post("/settings/providers/ai")
    async def save_ai_provider_settings(
        payload: AIProviderSettingsRequest,
        request: Request,
        db_session: DatabaseSession | AsyncDatabaseSession = Depends(get_db_dependency),
    ) -> Dict[str, Any]:
        tenant = request.state.tenant
        try:
            return await AIProviderService(db_session).save_settings(
                tenant=tenant,
                provider=payload.provider,
                api_key=payload.api_key,
                model=payload.model,
                enabled=payload.enabled,
            )
        except (AIProviderNotConfigured, SecretEncryptionError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/settings/providers/ai/status")
    async def ai_provider_status(
        request: Request,
        db_session: DatabaseSession | AsyncDatabaseSession = Depends(get_db_dependency),
    ) -> Dict[str, Any]:
        tenant = request.state.tenant
        try:
            return await AIProviderService(db_session).status(tenant)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/settings/providers/ai/test")
    async def test_ai_provider(
        request: Request,
        db_session: DatabaseSession | AsyncDatabaseSession = Depends(get_db_dependency),
    ) -> Dict[str, Any]:
        tenant = request.state.tenant
        try:
            return await AIProviderService(db_session).test_connection(tenant)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/dashboard/snapshot")
    async def dashboard_snapshot(
        request: Request,
        db_session: DatabaseSession | AsyncDatabaseSession = Depends(get_db_dependency),
    ) -> Dict[str, Any]:
        tenant = request.state.tenant
        return await LeadService(db_session).dashboard_snapshot(tenant)

    @app.post("/billing/subscribe")
    async def billing_subscribe(
        payload: SubscribeRequest,
        request: Request,
        db_session: DatabaseSession | AsyncDatabaseSession = Depends(get_db_dependency),
    ) -> Dict[str, Any]:
        tenant = request.state.tenant
        try:
            result = await BillingService(db_session).subscribe(tenant, payload.plan)
            return result
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/billing/upload-proof")
    async def billing_upload_proof(
        request: Request,
        reference_id: str = Form(...),
        proof_file: UploadFile = File(...),
        db_session: DatabaseSession | AsyncDatabaseSession = Depends(get_db_dependency),
    ) -> Dict[str, Any]:
        tenant = request.state.tenant
        service = BillingService(db_session)
        suffix = Path(proof_file.filename or "proof.bin").suffix.lower()
        allowed_suffixes = {".png", ".jpg", ".jpeg", ".pdf"}
        if suffix not in allowed_suffixes:
            raise HTTPException(status_code=400, detail="Unsupported proof file type.")
        content = await proof_file.read()
        if len(content) > 5 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="Proof file exceeds 5 MB limit.")
        destination = service.proof_storage_path(tenant, reference_id, proof_file.filename or "proof.bin")
        destination.write_bytes(content)
        try:
            payment = await service.upload_proof(tenant, reference_id, str(destination))
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return {
            "payment_reference_id": payment.payment_reference_id,
            "status": payment.status,
            "proof_url": payment.proof_url,
        }

    @app.post("/admin/payments/approve")
    async def admin_approve_payment(
        payload: ApprovePaymentRequest,
        request: Request,
        db_session: DatabaseSession | AsyncDatabaseSession = Depends(get_db_dependency),
    ) -> Dict[str, Any]:
        require_admin(request)
        tenant = request.state.tenant
        try:
            payment = await BillingService(db_session).approve_payment(tenant, payload.payment_reference_id)
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return {
            "payment_reference_id": payment.payment_reference_id,
            "status": payment.status,
            "tenant_id": tenant.tenant_id,
            "subscription_status": "active",
        }

    @app.get("/admin/summary")
    async def admin_summary(
        request: Request,
        db_session: DatabaseSession | AsyncDatabaseSession = Depends(get_db_dependency),
    ) -> Dict[str, Any]:
        require_admin(request)
        return await AdminAnalyticsService(db_session).summary()

    @app.get("/admin/users")
    async def admin_users(
        request: Request,
        db_session: DatabaseSession | AsyncDatabaseSession = Depends(get_db_dependency),
    ) -> Dict[str, Any]:
        require_admin(request)
        return {"items": await AdminAnalyticsService(db_session).recent_users(limit=50)}

    @app.get("/admin/tenants/usage")
    async def admin_tenant_usage(
        request: Request,
        db_session: DatabaseSession | AsyncDatabaseSession = Depends(get_db_dependency),
    ) -> Dict[str, Any]:
        require_admin(request)
        return {"items": await AdminAnalyticsService(db_session).tenant_usage(limit=100)}

    @app.get("/admin/leads/recent")
    async def admin_recent_leads(
        request: Request,
        db_session: DatabaseSession | AsyncDatabaseSession = Depends(get_db_dependency),
    ) -> Dict[str, Any]:
        require_admin(request)
        return {"items": await AdminAnalyticsService(db_session).recent_leads(limit=50)}

    @app.get("/admin/jobs/recent")
    async def admin_recent_jobs(
        request: Request,
        db_session: DatabaseSession | AsyncDatabaseSession = Depends(get_db_dependency),
    ) -> Dict[str, Any]:
        require_admin(request)
        return {"items": await AdminAnalyticsService(db_session).recent_jobs(limit=50)}

    return app


app = create_fastapi_app()
