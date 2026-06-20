"""Tenant management service."""

from __future__ import annotations

from app.core.models import Tenant
from app.db.session import AsyncDatabaseSession, DatabaseSession
from app.services._async import maybe_await


class TenantService:
    def __init__(self, db: DatabaseSession | AsyncDatabaseSession) -> None:
        self.db = db

    async def create_tenant(self, tenant_id: str, name: str, slug: str, subscription_plan: str = "Free") -> Tenant:
        tenant = Tenant(tenant_id=tenant_id, name=name, slug=slug, subscription_plan=subscription_plan)
        return await maybe_await(self.db.tenants.save(tenant))
