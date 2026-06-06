from __future__ import annotations

import pytest

from app.core.auth import verify_password
from app.core.models import Tenant, TenantContext, User
from app.db.session import build_memory_session
from app.services.admin_bootstrap_service import AdminBootstrapConfig, ensure_admin


def _config(email: str = "admin@example.test", password: str = "new-secret") -> AdminBootstrapConfig:
    return AdminBootstrapConfig(
        email=email,
        password=password,
        tenant_id="mian755",
        tenant_name="Lead Hunter AI Admin",
    )


@pytest.mark.anyio
async def test_admin_bootstrap_creates_admin_tenant_and_user() -> None:
    db = build_memory_session()

    result = await ensure_admin(db, _config())

    tenant = db.tenants.list("mian755")[0]
    user = db.users.find_by_email("mian755", "admin@example.test")
    assert result.email == "admin@example.test"
    assert result.tenant_id == "mian755"
    assert result.role == "admin"
    assert result.is_active is True
    assert tenant.name == "Lead Hunter AI Admin"
    assert tenant.subscription_plan == "Agency"
    assert tenant.is_active is True
    assert user is not None
    assert user.role == "admin"
    assert user.status == "active"
    assert verify_password("new-secret", user.password_hash)


@pytest.mark.anyio
async def test_admin_bootstrap_updates_existing_user_password_role_and_tenant() -> None:
    db = build_memory_session()
    db.tenants.save(Tenant(tenant_id="old-tenant", name="Old", slug="old", subscription_plan="Free"))
    db.tenants.save(Tenant(tenant_id="mian755", name="Wrong Name", slug="admin", subscription_plan="Free"))
    existing = db.for_tenant(TenantContext(tenant_id="old-tenant")).save(
        "users",
        User(
            tenant_id="old-tenant",
            email="admin@example.test",
            full_name="Existing Admin",
            password_hash="old-hash",
            role="member",
            status="pending",
        ),
    )

    result = await ensure_admin(db, _config(password="replacement-secret"))

    updated = db.users.find_by_email("mian755", "admin@example.test")
    old_lookup = db.users.find_by_email("old-tenant", "admin@example.test")
    tenant = db.tenants.list("mian755")[0]
    assert result == result.__class__(
        email="admin@example.test",
        tenant_id="mian755",
        role="admin",
        is_active=True,
    )
    assert updated is not None
    assert updated.id == existing.id
    assert updated.tenant_id == "mian755"
    assert updated.role == "admin"
    assert updated.status == "active"
    assert verify_password("replacement-secret", updated.password_hash)
    assert old_lookup is None
    assert tenant.name == "Lead Hunter AI Admin"
    assert tenant.subscription_plan == "Agency"


@pytest.mark.anyio
async def test_admin_bootstrap_leaves_normal_users_non_admin() -> None:
    db = build_memory_session()
    db.tenants.save(Tenant(tenant_id="tenant-normal", name="Normal", slug="normal", subscription_plan="Pro"))
    normal_user = db.for_tenant(TenantContext(tenant_id="tenant-normal")).save(
        "users",
        User(
            tenant_id="tenant-normal",
            email="normal@example.test",
            full_name="Normal User",
            password_hash="normal-hash",
            role="owner",
            status="active",
        ),
    )

    await ensure_admin(db, _config())

    unchanged = db.users.find_by_email("tenant-normal", "normal@example.test")
    assert unchanged is not None
    assert unchanged.id == normal_user.id
    assert unchanged.role == "owner"
    assert unchanged.tenant_id == "tenant-normal"
    assert unchanged.password_hash == "normal-hash"
