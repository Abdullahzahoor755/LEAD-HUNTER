"""Top-level application facade used by APIs, workers, and UIs."""

from __future__ import annotations

from dataclasses import dataclass

from app.api.middleware import AuthMiddleware
from app.api.app import ApiApplication
from app.core.tenant import TenantMiddleware
from app.db.session import DatabaseSession
from app.runtime.session import get_runtime
from app.workers.runner import build_job_queue


@dataclass(slots=True)
class ApplicationRuntime:
    db: DatabaseSession
    api: ApiApplication
    tenant_middleware: TenantMiddleware
    auth_middleware: AuthMiddleware


def build_runtime() -> ApplicationRuntime:
    shared = get_runtime()
    db = shared.db
    return ApplicationRuntime(
        db=db,
        api=ApiApplication(db=db),
        tenant_middleware=TenantMiddleware(),
        auth_middleware=AuthMiddleware(),
    )


def build_runtime_with_queue():
    shared = get_runtime()
    runtime = build_runtime()
    queue = getattr(shared, "queue", build_job_queue(runtime.db))
    return runtime, queue
