"""Idempotent admin tenant/user bootstrap helpers."""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Sequence

from app.core.auth import hash_password
from app.core.models import Tenant, User
from app.db.session import AsyncDatabaseSession, DatabaseSession
from app.services._async import maybe_await


DEFAULT_ADMIN_TENANT_ID = "mian755"
DEFAULT_ADMIN_TENANT_NAME = "Lead Hunter AI Admin"
DEFAULT_ADMIN_TENANT_SLUG = "admin"
DEFAULT_ADMIN_FULL_NAME = "Admin User"


@dataclass(slots=True)
class AdminBootstrapConfig:
    email: str
    password: str
    tenant_id: str = DEFAULT_ADMIN_TENANT_ID
    tenant_name: str = DEFAULT_ADMIN_TENANT_NAME
    tenant_slug: str = DEFAULT_ADMIN_TENANT_SLUG
    full_name: str = DEFAULT_ADMIN_FULL_NAME


@dataclass(slots=True)
class AdminBootstrapResult:
    email: str
    tenant_id: str
    role: str
    is_active: bool


def has_admin_bootstrap_env() -> bool:
    return bool(os.getenv("ADMIN_EMAIL", "").strip() and os.getenv("ADMIN_PASSWORD", ""))


def admin_bootstrap_config_from_env() -> AdminBootstrapConfig:
    email = os.getenv("ADMIN_EMAIL", "").strip().lower()
    password = os.getenv("ADMIN_PASSWORD", "")
    if not email or not password:
        raise ValueError("ADMIN_EMAIL and ADMIN_PASSWORD are required to bootstrap admin.")
    tenant_id = os.getenv("ADMIN_TENANT_ID", DEFAULT_ADMIN_TENANT_ID).strip() or DEFAULT_ADMIN_TENANT_ID
    tenant_name = os.getenv("ADMIN_TENANT_NAME", DEFAULT_ADMIN_TENANT_NAME).strip() or DEFAULT_ADMIN_TENANT_NAME
    tenant_slug = os.getenv("ADMIN_TENANT_SLUG", DEFAULT_ADMIN_TENANT_SLUG).strip() or DEFAULT_ADMIN_TENANT_SLUG
    full_name = os.getenv("ADMIN_FULL_NAME", DEFAULT_ADMIN_FULL_NAME).strip() or DEFAULT_ADMIN_FULL_NAME
    return AdminBootstrapConfig(
        email=email,
        password=password,
        tenant_id=tenant_id,
        tenant_name=tenant_name,
        tenant_slug=tenant_slug,
        full_name=full_name,
    )


async def ensure_admin_from_env(db: DatabaseSession | AsyncDatabaseSession) -> AdminBootstrapResult:
    return await ensure_admin(db, admin_bootstrap_config_from_env())


async def ensure_admin(
    db: DatabaseSession | AsyncDatabaseSession,
    config: AdminBootstrapConfig,
) -> AdminBootstrapResult:
    tenant = await _ensure_admin_tenant(db, config)
    user, old_tenant_id = await _admin_user_for_email(db, config.email, config.tenant_id)
    if user is None:
        user = User(
            tenant_id=tenant.tenant_id,
            email=config.email.strip().lower(),
            full_name=config.full_name.strip(),
        )
    user.tenant_id = tenant.tenant_id
    user.email = config.email.strip().lower()
    user.full_name = user.full_name.strip() or config.full_name.strip()
    user.password_hash = hash_password(config.password)
    user.role = "admin"
    user.status = "active"
    metadata = dict(user.metadata or {})
    metadata["subscription_plan"] = "Agency"
    user.metadata = metadata
    if old_tenant_id and old_tenant_id != tenant.tenant_id:
        await maybe_await(db.users.delete(old_tenant_id, user.id))
    await maybe_await(db.users.save(user))
    return AdminBootstrapResult(
        email=user.email,
        tenant_id=user.tenant_id,
        role=user.role,
        is_active=user.status.lower() == "active",
    )


async def _ensure_admin_tenant(
    db: DatabaseSession | AsyncDatabaseSession,
    config: AdminBootstrapConfig,
) -> Tenant:
    records = await maybe_await(db.tenants.list(config.tenant_id))
    tenant = records[0] if records else Tenant(tenant_id=config.tenant_id)
    tenant.name = config.tenant_name
    tenant.slug = config.tenant_slug
    tenant.status = "active"
    tenant.is_active = True
    tenant.subscription_plan = "Agency"
    tenant.subscription_status = "active"
    tenant.settings = dict(tenant.settings or {})
    return await maybe_await(db.tenants.save(tenant))


async def _admin_user_for_email(
    db: DatabaseSession | AsyncDatabaseSession,
    email: str,
    tenant_id: str,
) -> tuple[User | None, str]:
    normalized_email = email.strip().lower()
    users: Sequence[User] = await maybe_await(db.users.list_all())
    matches = [user for user in users if user.email.strip().lower() == normalized_email]
    if not matches:
        return None, ""
    for user in matches:
        if user.tenant_id == tenant_id:
            return user, user.tenant_id
    user = matches[0]
    return user, user.tenant_id
