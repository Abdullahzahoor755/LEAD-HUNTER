"""Subscription plan feature gates."""

from __future__ import annotations

from app.core.auth import has_pro_features
from app.core.models import TenantContext
from app.db.session import AsyncDatabaseSession, DatabaseSession
from app.services._async import maybe_await


class PlanGateError(ValueError):
    """Raised when a tenant's subscription plan does not allow a feature."""


async def require_pro_plan(
    db: DatabaseSession | AsyncDatabaseSession,
    tenant: TenantContext,
    message: str = "Outreach is available in Pro plan.",
) -> None:
    tenants = await maybe_await(db.tenants.list(tenant.tenant_id))
    plan = str(tenants[0].subscription_plan if tenants else "").strip()
    if not has_pro_features(plan):
        raise PlanGateError(message)
