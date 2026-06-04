"""Simple API facade and FastAPI app for the multi-tenant SaaS runtime."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
import logging
import os
from pathlib import Path
from typing import Any, Dict, Sequence

from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.configs.settings import settings
from app.middleware.auth import AuthTenantMiddleware
from app.middleware.security import InMemoryRateLimitMiddleware, SecurityHeadersMiddleware
from app.core.auth import get_plan_limits, is_plan_gated_agent, validate_production_jwt_secret
from app.core.models import Job, Lead, TenantContext
from app.core.tenant import get_current_tenant, resolve_tenant_context
from app.db.postgres import initialize_async_database, verify_async_database
from app.db.session import AsyncDatabaseSession, DatabaseSession, get_async_db_session, reset_async_session_factory
from app.services.auth_service import AuthService
from app.services.admin_analytics_service import AdminAnalyticsService
from app.services.billing_service import BillingService
from app.services.job_service import JobService
from app.services.lead_service import LeadService
from app.services.plan_gate import PlanGateError, require_pro_plan
from app.services.provider_credential_service import ProviderCredentialService
from app.services.security_service import SecretEncryptionError
from app.services._async import maybe_await

LOGGER = logging.getLogger(__name__)


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


class RunOnceRequest(BaseModel):
    job_type: str = ""


class SubscribeRequest(BaseModel):
    plan: str


class ApprovePaymentRequest(BaseModel):
    payment_reference_id: str


def _serialize_lead(lead: Lead) -> Dict[str, Any]:
    last_reply_at = lead.last_reply_at
    return {
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
        db_session: DatabaseSession | AsyncDatabaseSession = Depends(get_db_dependency),
    ) -> Dict[str, Any]:
        tenant = request.state.tenant
        items = await LeadService(db_session).list_leads(tenant)
        return {
            "tenant_id": tenant.tenant_id,
            "items": [_serialize_lead(item) for item in items],
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
            "scopes": ["https://www.googleapis.com/auth/gmail.send"],
        }
        try:
            await ProviderCredentialService(db_session).save_gmail_credentials(tenant, credentials)
        except SecretEncryptionError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {
            "configured": True,
            "sender_email": credentials["email_address"],
        }

    @app.get("/settings/providers/gmail/status")
    async def gmail_provider_status(
        request: Request,
        db_session: DatabaseSession | AsyncDatabaseSession = Depends(get_db_dependency),
    ) -> Dict[str, Any]:
        tenant = request.state.tenant
        await require_pro_features(tenant, db_session, "Gmail automation is a Pro feature.")
        credentials = await ProviderCredentialService(db_session).get_gmail_credentials(tenant)
        sender_email = str(credentials.get("email_address", "")).strip().lower() if credentials else ""
        return {
            "configured": bool(credentials),
            "sender_email": sender_email,
        }

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
