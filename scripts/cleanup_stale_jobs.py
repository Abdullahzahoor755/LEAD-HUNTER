#!/usr/bin/env python
"""Cancel stale queued jobs for one tenant in local/dev environments."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.models import TenantContext
from app.db.session import get_async_db_session
from app.services.stale_data_cleanup import cleanup_stale_jobs


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
    parser = argparse.ArgumentParser(description="Dry-run or cancel stale queued jobs for a single tenant.")
    parser.add_argument("--tenant", required=True, help="Tenant ID to inspect.")
    parser.add_argument("--apply", action="store_true", help="Apply the cleanup. Omit for dry-run.")
    parser.add_argument("--dry-run", action="store_true", help="Report only. This is the default.")
    parser.add_argument("--older-than-days", type=int, default=1, help="Queued-job age cutoff in days. Default: 1.")
    parser.add_argument("--before", default="", help="ISO timestamp cutoff. Overrides --older-than-days.")
    parser.add_argument("--status", choices=["cancelled", "failed"], default="cancelled", help="Status to assign in apply mode.")
    parser.add_argument("--allow-production", action="store_true", help="Allow --apply when APP_ENV=production.")
    return parser.parse_args()


def parse_cutoff(value: str) -> datetime | None:
    if not value.strip():
        return None
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def enforce_safety(args: argparse.Namespace) -> None:
    if args.dry_run and args.apply:
        raise SystemExit("Choose either --dry-run or --apply, not both.")
    if args.apply and os.getenv("APP_ENV", "development").strip().lower() == "production" and not args.allow_production:
        raise SystemExit("--apply is blocked when APP_ENV=production. Pass --allow-production only if you really intend this.")


async def main() -> None:
    load_env_file()
    args = parse_args()
    enforce_safety(args)
    tenant = TenantContext(tenant_id=args.tenant)
    async with get_async_db_session() as db:
        report = await cleanup_stale_jobs(
            db,
            tenant,
            cutoff=parse_cutoff(args.before),
            older_than_days=args.older_than_days,
            apply=args.apply,
            status=args.status,
        )
    print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    if not args.apply:
        print("Dry-run only: no jobs were modified. Re-run with --apply to mark stale queued jobs.")
    else:
        print(f"Apply complete: marked {report.cancelled_count} queued job(s) as {args.status}.")


if __name__ == "__main__":
    asyncio.run(main())
