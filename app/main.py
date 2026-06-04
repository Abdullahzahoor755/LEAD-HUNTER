"""Example bootstrap for the multi-tenant SaaS runtime."""

from __future__ import annotations

import asyncio

from app.configs.settings import settings
from app.core.models import TenantContext
from app.db.postgres import initialize_async_database, verify_async_database
from app.services.application_service import build_runtime_with_queue
from app.services.job_service import JobService
from app.services.tenant_service import TenantService


async def bootstrap_demo() -> dict:
    if settings.database_backend == "postgres":
        await initialize_async_database()
    runtime, queue = build_runtime_with_queue()
    tenant_service = TenantService(runtime.db)
    await tenant_service.create_tenant(tenant_id="tenant-demo", name="Demo Tenant", slug="demo")

    tenant = TenantContext(tenant_id="tenant-demo", tenant_slug="demo")
    job = await JobService(runtime.db).enqueue(
        tenant=tenant,
        name="lead_generation",
        payload={"limit": 1, "query": "software companies in Riyadh"},
    )
    await queue.enqueue(job)
    return await queue.run_once()


def main() -> None:
    result = asyncio.run(bootstrap_demo())
    print(result)


async def check_database() -> dict:
    if settings.database_backend != "postgres":
        return {"ok": True, "backend": settings.database_backend}
    await initialize_async_database()
    return await verify_async_database()


if __name__ == "__main__":
    main()
