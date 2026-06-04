"""Frontend application entrypoint for tenant-aware dashboards."""

from __future__ import annotations

from app.frontend.dashboard import build_dashboard_context
from app.services.application_service import build_runtime


def get_dashboard_snapshot(tenant_id: str) -> dict:
    runtime = build_runtime()
    return build_dashboard_context(runtime.api, tenant_id)
