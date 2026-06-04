"""Frontend helpers for tenant-aware dashboards."""

from __future__ import annotations

import asyncio
from typing import Dict

from app.api.app import ApiApplication


def build_dashboard_context(api: ApiApplication, tenant_id: str) -> Dict[str, object]:
    leads = asyncio.run(api.list_leads(tenant_id))
    return {
        "tenant_id": tenant_id,
        "total_leads": len(leads),
        "sent_leads": sum(1 for lead in leads if lead.status.lower() == "sent"),
        "pending_leads": sum(1 for lead in leads if lead.status.lower() == "pending"),
    }
