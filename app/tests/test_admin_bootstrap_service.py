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
    assert result.slug == "mian755"
    assert result.role == "admin"
    assert result.is_active is True
    assert tenant.name == "Lead Hunter AI Admin"
    assert tenant.slug == "mian755"
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
    db.tenants.save(Tenant(tenant_id="mian755", name="Wrong Name", slug="existing-admin-slug", subscription_plan="Free"))
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
        slug="existing-admin-slug",
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
    assert tenant.slug == "existing-admin-slug"
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


@pytest.mark.anyio
async def test_admin_bootstrap_new_tenant_slug_collision_uses_suffix_without_touching_existing() -> None:
    db = build_memory_session()
    existing_slug_owner = db.tenants.save(
        Tenant(tenant_id="other-admin", name="Other Admin", slug="mian755", subscription_plan="Free")
    )
    second_collision = db.tenants.save(
        Tenant(tenant_id="other-admin-2", name="Other Admin 2", slug="mian755-1", subscription_plan="Pro")
    )

    result = await ensure_admin(db, _config())

    admin_tenant = db.tenants.list("mian755")[0]
    untouched_one = db.tenants.list("other-admin")[0]
    untouched_two = db.tenants.list("other-admin-2")[0]
    assert result.slug == "mian755-2"
    assert admin_tenant.slug == "mian755-2"
    assert untouched_one.id == existing_slug_owner.id
    assert untouched_one.slug == "mian755"
    assert untouched_one.subscription_plan == "Free"
    assert untouched_two.id == second_collision.id
    assert untouched_two.slug == "mian755-1"
    assert untouched_two.subscription_plan == "Pro"


@pytest.mark.anyio
async def test_admin_bootstrap_existing_tenant_preserves_slug_even_if_admin_slug_exists_elsewhere() -> None:
    db = build_memory_session()
    db.tenants.save(Tenant(tenant_id="legacy-admin", name="Legacy", slug="admin", subscription_plan="Free"))
    db.tenants.save(Tenant(tenant_id="mian755", name="Old Admin", slug="admin-panel", subscription_plan="Free"))

    result = await ensure_admin(db, _config())

    admin_tenant = db.tenants.list("mian755")[0]
    legacy_tenant = db.tenants.list("legacy-admin")[0]
    assert result.slug == "admin-panel"
    assert admin_tenant.slug == "admin-panel"
    assert admin_tenant.name == "Lead Hunter AI Admin"
    assert admin_tenant.subscription_plan == "Agency"
    assert legacy_tenant.slug == "admin"
    assert legacy_tenant.subscription_plan == "Free"


@pytest.mark.anyio
async def test_admin_bootstrap_repeated_runs_are_idempotent_and_update_password() -> None:
    db = build_memory_session()

    first = await ensure_admin(db, _config(password="first-secret"))
    second = await ensure_admin(db, _config(password="second-secret"))

    tenants = db.tenants.list("mian755")
    users = [user for user in db.users.list_all() if user.email == "admin@example.test"]
    user = db.users.find_by_email("mian755", "admin@example.test")
    assert first.tenant_id == second.tenant_id == "mian755"
    assert first.slug == second.slug == "mian755"
    assert len(tenants) == 1
    assert len(users) == 1
    assert user is not None
    assert user.role == "admin"
    assert user.status == "active"
    assert verify_password("second-secret", user.password_hash)
    assert not verify_password("first-secret", user.password_hash)
