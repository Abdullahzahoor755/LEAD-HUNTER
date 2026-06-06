#!/usr/bin/env python
"""Backfill missing outreach_error metadata for failed leads."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.models import TenantContext
from app.db.session import AsyncDatabaseSession, DatabaseSession, get_async_db_session
from app.services._async import maybe_await


BACKFILL_ERROR = "unknown_outreach_failure"


def load_env_file() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


async def backfill_outreach_errors(
    db: DatabaseSession | AsyncDatabaseSession,
    tenant_id: str,
    *,
    apply: bool = False,
) -> Dict[str, Any]:
    tenant = TenantContext(tenant_id=tenant_id)
    scoped = db.for_tenant(tenant)
    leads = await maybe_await(scoped.list("leads"))
    matched = []
    changed = 0
    now_iso = datetime.now(timezone.utc).isoformat()
    for lead in leads:
        status = str(getattr(lead, "status", "") or "").strip().lower()
        outreach_status = str(getattr(lead, "outreach_status", "") or "").strip().lower()
        metadata = dict(getattr(lead, "metadata", {}) or {})
        if status != "failed" and outreach_status != "failed":
            continue
        if str(metadata.get("outreach_error", "") or "").strip():
            continue
        matched.append({"lead_id": lead.id, "company_url": lead.company_url, "email": lead.email or lead.verified_email})
        if apply:
            metadata["outreach_error"] = BACKFILL_ERROR
            metadata.setdefault("outreach_error_at", now_iso)
            lead.metadata = metadata
            await maybe_await(scoped.save("leads", lead))
            changed += 1
    return {
        "tenant_id": tenant_id,
        "dry_run": not apply,
        "matched_count": len(matched),
        "updated_count": changed,
        "error": BACKFILL_ERROR,
        "items": matched[:25],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dry-run or backfill missing outreach_error for failed leads.")
    parser.add_argument("--tenant", required=True, help="Tenant ID to inspect.")
    parser.add_argument("--apply", action="store_true", help="Apply the backfill. Omit for dry-run.")
    return parser.parse_args()


async def main() -> None:
    load_env_file()
    args = parse_args()
    async with get_async_db_session() as db:
        report = await backfill_outreach_errors(db, args.tenant, apply=args.apply)
    print(json.dumps(report, indent=2, sort_keys=True))
    if not args.apply:
        print("Dry-run only: no leads were modified. Re-run with --apply to backfill outreach_error.")
    else:
        print(f"Apply complete: updated {report['updated_count']} failed lead(s).")


if __name__ == "__main__":
    asyncio.run(main())
