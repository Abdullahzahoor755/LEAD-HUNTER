"""Signup, login, JWT issuance, and plan-aware usage enforcement."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict

from app.core.auth import create_jwt_token, decode_jwt_token, get_plan_limits, hash_password, normalize_subscription_plan, verify_password
from app.core.models import Tenant, TenantContext, User
from app.core.tenant import TenantIsolationError, assert_same_tenant, require_tenant_id
from app.db.session import AsyncDatabaseSession, DatabaseSession
from app.services._async import maybe_await
from app.services.tenant_service import TenantService


@dataclass(slots=True)
class AuthResult:
    tenant_id: str
    user_id: str
    email: str
    token: str
    subscription_plan: str
    usage_limits: Dict[str, int]


class UsageLimitExceededError(ValueError):
    """Raised when a tenant exceeds its subscription plan limits."""


class AuthenticationError(ValueError):
    """Raised when authentication fails."""


class AuthService:
    def __init__(self, db: DatabaseSession | AsyncDatabaseSession) -> None:
        self.db = db
        self.tenant_service = TenantService(db)

    async def signup(
        self,
        tenant_id: str,
        tenant_name: str,
        tenant_slug: str,
        email: str,
        password: str,
        full_name: str,
        plan: str = "Starter",
        role: str = "owner",
    ) -> AuthResult:
        resolved_tenant_id = require_tenant_id(tenant_id)
        normalized_plan = normalize_subscription_plan(plan)
        existing_user = await maybe_await(self.db.users.find_by_email(resolved_tenant_id, email))
        if existing_user:
            raise AuthenticationError("A user with this email already exists in the tenant.")

        tenant = await self._find_tenant(resolved_tenant_id)
        if tenant is None:
            tenant = await self.tenant_service.create_tenant(
                tenant_id=resolved_tenant_id,
                name=tenant_name,
                slug=tenant_slug,
                subscription_plan=normalized_plan,
            )

        await self._enforce_team_member_limit(tenant)
        user = User(
            tenant_id=resolved_tenant_id,
            email=email.strip().lower(),
            full_name=full_name.strip(),
            password_hash=hash_password(password),
            role=role,
            status="active",
            metadata={"subscription_plan": normalized_plan},
        )
        await maybe_await(self.db.for_tenant(TenantContext(tenant_id=resolved_tenant_id)).save("users", user))
        return self._build_auth_result(tenant, user)

    async def login(self, tenant_id: str, email: str, password: str) -> AuthResult:
        resolved_tenant_id = require_tenant_id(tenant_id)
        user = await maybe_await(self.db.users.find_by_email(resolved_tenant_id, email))
        if user is None or not verify_password(password, user.password_hash):
            raise AuthenticationError("Invalid email or password.")
        if user.status.lower() != "active":
            raise AuthenticationError("User account is not active.")
        tenant = await self._find_tenant(resolved_tenant_id)
        if tenant is None:
            raise AuthenticationError("Tenant does not exist.")
        return self._build_auth_result(tenant, user)

    async def authenticate_token(self, token: str) -> Dict[str, Any]:
        payload = decode_jwt_token(token)
        tenant_id = require_tenant_id(str(payload.get("tenant_id", "")))
        user_id = str(payload.get("user_id", "")).strip()
        user = await maybe_await(self.db.users.get(tenant_id, user_id))
        if user is None:
            raise AuthenticationError("Token user not found.")
        assert_same_tenant(tenant_id, user.tenant_id)
        return payload

    async def enforce_usage_limit(self, tenant: Tenant, metric: str, amount: int = 1) -> Dict[str, int]:
        limits = get_plan_limits(tenant.subscription_plan or "Starter")
        if metric not in limits:
            raise UsageLimitExceededError(f"Unknown usage metric: {metric}")
        usage = dict(tenant.settings.get("usage", {}))
        current = int(usage.get(metric, 0) or 0)
        proposed = current + int(amount)
        if proposed > limits[metric]:
            raise UsageLimitExceededError(
                f"Plan limit exceeded for {metric}: {proposed}/{limits[metric]} on {tenant.subscription_plan}."
            )
        usage[metric] = proposed
        tenant.settings["usage"] = usage
        await maybe_await(self.db.tenants.save(tenant))
        return {"metric": metric, "used": proposed, "limit": limits[metric]}

    async def _find_tenant(self, tenant_id: str) -> Tenant | None:
        tenants = await maybe_await(self.db.tenants.list(tenant_id))
        return tenants[0] if tenants else None

    async def _enforce_team_member_limit(self, tenant: Tenant) -> None:
        limits = get_plan_limits(tenant.subscription_plan or "Starter")
        existing_users = await maybe_await(self.db.users.list(tenant.tenant_id))
        if len(existing_users) >= limits["team_members"]:
            raise UsageLimitExceededError(
                f"Plan limit exceeded for team_members: {len(existing_users)}/{limits['team_members']} on {tenant.subscription_plan}."
            )

    def _build_auth_result(self, tenant: Tenant, user: User) -> AuthResult:
        if tenant.tenant_id != user.tenant_id:
            raise TenantIsolationError("User tenant does not match tenant record.")
        token = create_jwt_token(
            {
                "tenant_id": tenant.tenant_id,
                "tenant_slug": tenant.slug,
                "user_id": user.id,
                "email": user.email,
                "role": user.role,
                "plan": tenant.subscription_plan,
            }
        )
        return AuthResult(
            tenant_id=tenant.tenant_id,
            user_id=user.id,
            email=user.email,
            token=token,
            subscription_plan=tenant.subscription_plan,
            usage_limits=get_plan_limits(tenant.subscription_plan),
        )
