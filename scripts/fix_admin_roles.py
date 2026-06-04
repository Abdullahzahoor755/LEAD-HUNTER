#!/usr/bin/env python
"""Dry-run or fix admin roles so only the selected main admin remains admin."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import sys
from typing import Any, List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

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


load_env_file()

if __name__ == "__main__" and os.getenv("FIX_ADMIN_ROLES_TRAMPOLINE") != "1":
    env = dict(os.environ)
    env["FIX_ADMIN_ROLES_TRAMPOLINE"] = "1"
    command = (
        "import asyncio, sys; "
        "from scripts.fix_admin_roles import main; "
        "asyncio.run(main())"
    )
    os.execve(sys.executable, [sys.executable, "-c", command, *sys.argv[1:]], env)

from app.core.models import TenantContext, User
from app.db.session import AsyncDatabaseSession, DatabaseSession, get_async_db_session
from app.services._async import maybe_await


@dataclass(slots=True)
class AdminRoleRow:
    user_id: str
    email: str
    tenant_id: str
    role: str
    matches_identifier: bool
    action: str


@dataclass(slots=True)
class AdminRoleReport:
    admin_identifier: str
    apply: bool
    selected_admin_count: int
    demoted_count: int
    rows: List[AdminRoleRow]

    def to_dict(self) -> dict[str, Any]:
        return {
            "admin_identifier": self.admin_identifier,
            "apply": self.apply,
            "selected_admin_count": self.selected_admin_count,
            "demoted_count": self.demoted_count,
            "rows": [asdict(row) for row in self.rows],
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ensure only the selected main admin keeps admin privileges.")
    parser.add_argument("--admin-identifier", default="mian755", help="Email/tenant/metadata substring identifying the main admin.")
    parser.add_argument("--apply", action="store_true", help="Apply role changes. Omit for dry-run.")
    parser.add_argument("--dry-run", action="store_true", help="Report only. This is the default.")
    parser.add_argument("--allow-production", action="store_true", help="Allow --apply when APP_ENV=production.")
    return parser.parse_args()


def enforce_safety(args: argparse.Namespace) -> None:
    if args.dry_run and args.apply:
        raise SystemExit("Choose either --dry-run or --apply, not both.")
    if args.apply and os.getenv("APP_ENV", "development").strip().lower() == "production" and not args.allow_production:
        raise SystemExit("--apply is blocked when APP_ENV=production. Pass --allow-production only if you really intend this.")


def is_admin_user_record(user: User) -> bool:
    metadata = user.metadata or {}
    metadata_admin = str(metadata.get("is_admin", "")).strip().lower() in {"1", "true", "yes", "on"}
    return str(user.role or "").strip().lower() == "admin" or metadata_admin


def matches_identifier(user: User, identifier: str) -> bool:
    needle = str(identifier or "").strip().lower()
    metadata = user.metadata or {}
    haystack = " ".join([user.id, user.email, user.tenant_id, str(metadata)]).lower()
    return bool(needle and needle in haystack)


async def fix_admin_roles(
    db: DatabaseSession | AsyncDatabaseSession,
    admin_identifier: str = "mian755",
    apply: bool = False,
) -> AdminRoleReport:
    tenants = await maybe_await(db.tenants.list_all())
    rows: List[AdminRoleRow] = []
    selected_admin_count = 0
    demoted_count = 0
    for tenant in tenants:
        users = await maybe_await(db.users.list(tenant.tenant_id))
        for user in users:
            if not is_admin_user_record(user):
                continue
            matched = matches_identifier(user, admin_identifier)
            if matched:
                selected_admin_count += 1
                action = "keep_admin"
                if apply and str(user.role or "").strip().lower() != "admin":
                    user.role = "admin"
                    await maybe_await(db.for_tenant(TenantContext(tenant_id=user.tenant_id)).save("users", user))
            else:
                action = "would_demote"
                if apply:
                    metadata = dict(user.metadata or {})
                    metadata.setdefault("previous_admin_role", user.role)
                    metadata.pop("is_admin", None)
                    user.metadata = metadata
                    user.role = "member"
                    user.status = user.status or "active"
                    await maybe_await(db.for_tenant(TenantContext(tenant_id=user.tenant_id)).save("users", user))
                    action = "demoted"
                    demoted_count += 1
            rows.append(
                AdminRoleRow(
                    user_id=user.id,
                    email=user.email,
                    tenant_id=user.tenant_id,
                    role=user.role,
                    matches_identifier=matched,
                    action=action,
                )
            )
    return AdminRoleReport(
        admin_identifier=admin_identifier,
        apply=apply,
        selected_admin_count=selected_admin_count,
        demoted_count=demoted_count,
        rows=rows,
    )


async def main() -> None:
    load_env_file()
    args = parse_args()
    enforce_safety(args)
    async with get_async_db_session() as db:
        report = await fix_admin_roles(db, admin_identifier=args.admin_identifier, apply=args.apply)
    print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    if args.apply:
        print(f"Apply complete: demoted {report.demoted_count} non-selected admin user(s).")
    else:
        print("Dry-run only: no users were modified. Re-run with --apply to demote non-selected admins.")
    sys.stdout.flush()


if __name__ == "__main__":
    asyncio.run(main())
