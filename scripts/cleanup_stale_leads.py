#!/usr/bin/env python
"""Clean stale persisted leads for one tenant in local/dev environments."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.models import TenantContext
from app.db.session import get_async_db_session
from app.services.stale_data_cleanup import cleanup_stale_leads


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dry-run or clean stale leads for a single tenant.")
    parser.add_argument("--tenant", required=True, help="Tenant ID to inspect.")
    parser.add_argument("--apply", action="store_true", help="Apply the cleanup. Omit for dry-run.")
    parser.add_argument("--dry-run", action="store_true", help="Report only. This is the default.")
    parser.add_argument("--delete-stale", action="store_true", help="Delete stale rows instead of cleaning fields.")
    parser.add_argument("--allow-production", action="store_true", help="Allow --apply when APP_ENV=production.")
    return parser.parse_args()


def enforce_safety(args: argparse.Namespace) -> None:
    if args.dry_run and args.apply:
        raise SystemExit("Choose either --dry-run or --apply, not both.")
    if args.delete_stale and not args.apply:
        print("--delete-stale requested without --apply; reporting rows that would be deleted.")
    if args.apply and os.getenv("APP_ENV", "development").strip().lower() == "production" and not args.allow_production:
        raise SystemExit("--apply is blocked when APP_ENV=production. Pass --allow-production only if you really intend this.")


async def main() -> None:
    load_env_file()
    args = parse_args()
    enforce_safety(args)
    tenant = TenantContext(tenant_id=args.tenant)
    async with get_async_db_session() as db:
        report = await cleanup_stale_leads(db, tenant, apply=args.apply, delete_stale=args.delete_stale)
    print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    if not args.apply:
        print("Dry-run only: no rows were modified. Re-run with --apply to clean, or --apply --delete-stale to delete stale rows.")
    elif args.delete_stale:
        print(f"Apply complete: deleted {report.deleted_count} stale lead row(s).")
    else:
        print(f"Apply complete: cleaned {report.cleaned_count} stale lead row(s).")


if __name__ == "__main__":
    asyncio.run(main())
