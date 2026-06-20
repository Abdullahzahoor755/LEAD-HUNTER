"""Route-like functions that can later be mounted on FastAPI or another web framework."""

from __future__ import annotations

from typing import Any, Dict, Sequence

from app.api.app import ApiApplication
from app.core.models import Lead
from app.core.tenant import TenantMiddleware


TENANT_MIDDLEWARE = TenantMiddleware()


async def get_leads(api: ApiApplication, tenant_id: str = "") -> Sequence[Lead]:
    return await api.list_leads(tenant_id=tenant_id)


async def post_agent_job(api: ApiApplication, tenant_id: str, agent_name: str, payload: Dict[str, Any]) -> Dict[str, str]:
    return await api.enqueue_agent_job(tenant_id=tenant_id, agent_name=agent_name, payload=payload)


async def post_signup(
    api: ApiApplication,
    tenant_id: str,
    tenant_name: str,
    tenant_slug: str,
    email: str,
    password: str,
    full_name: str,
    plan: str = "Free",
) -> Dict[str, Any]:
    return await api.signup(
        tenant_id=tenant_id,
        tenant_name=tenant_name,
        tenant_slug=tenant_slug,
        email=email,
        password=password,
        full_name=full_name,
        plan=plan,
    )


async def post_login(api: ApiApplication, tenant_id: str, email: str, password: str) -> Dict[str, Any]:
    return await api.login(tenant_id=tenant_id, email=email, password=password)


def get_plan_limits(api: ApiApplication, plan: str) -> Dict[str, int]:
    return api.plan_limits(plan)


async def post_consume_usage(api: ApiApplication, tenant_id: str, metric: str, amount: int = 1) -> Dict[str, int]:
    return await api.consume_usage(tenant_id=tenant_id, metric=metric, amount=amount)


async def get_leads_from_request(api: ApiApplication, request: Any) -> Sequence[Lead]:
    tenant = TENANT_MIDDLEWARE.resolve(request)
    return await api.list_leads(tenant_id=tenant.tenant_id, tenant_slug=tenant.tenant_slug, user_id=tenant.user_id)


async def post_agent_job_from_request(
    api: ApiApplication,
    request: Any,
    agent_name: str,
    payload: Dict[str, Any],
) -> Dict[str, str]:
    tenant = TENANT_MIDDLEWARE.resolve(request)
    return await api.enqueue_agent_job(
        tenant_id=tenant.tenant_id,
        tenant_slug=tenant.tenant_slug,
        user_id=tenant.user_id,
        agent_name=agent_name,
        payload=payload,
    )
