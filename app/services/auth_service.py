"""Signup, login, JWT issuance, and plan-aware usage enforcement."""

from __future__ import annotations

from dataclasses import dataclass
import re
import secrets
from typing import Any, Dict, Sequence
from uuid import uuid4

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
    role: str
    plan: str
    token: str
    subscription_plan: str
    usage_limits: Dict[str, int]


class UsageLimitExceededError(ValueError):
    """Raised when a tenant exceeds its subscription plan limits."""


class AuthenticationError(ValueError):
    """Raised when authentication fails."""


class TenantNameAlreadyTakenError(ValueError):
    """Raised when public signup tries to use an existing tenant identity."""


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
        plan: str = "Free",
        role: str = "owner",
    ) -> AuthResult:
        organization_name = str(tenant_name or tenant_slug or tenant_id or "").strip()
        if not organization_name:
            raise AuthenticationError("Organization name is required.")
        await self._ensure_public_signup_identity_available(
            requested_tenant_id=tenant_id,
            tenant_name=organization_name,
            tenant_slug=tenant_slug,
        )
        resolved_tenant_id = tenant_id.strip() if str(tenant_id or "").strip() else uuid4().hex
        normalized_slug = await self._unique_public_signup_slug(organization_name)
        normalized_plan = normalize_subscription_plan(plan)
        tenant = await self.tenant_service.create_tenant(
            tenant_id=resolved_tenant_id,
            name=organization_name,
            slug=normalized_slug,
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

    async def google_login_or_signup(
        self,
        email: str,
        full_name: str = "",
        google_sub: str = "",
        picture: str = "",
        email_verified: bool = False,
    ) -> AuthResult:
        normalized_email = str(email or "").strip().lower()
        if not normalized_email or "@" not in normalized_email:
            raise AuthenticationError("Google account did not return a valid email.")
        if not email_verified:
            raise AuthenticationError("Google email is not verified.")

        existing = await self._find_user_by_email_globally(normalized_email)
        if existing is not None:
            tenant, user = existing
            if user.status.lower() != "active":
                raise AuthenticationError("User account is not active.")
            metadata = dict(user.metadata or {})
            metadata["google_auth"] = {
                "sub": str(google_sub or metadata.get("google_sub", "") or ""),
                "email_verified": True,
                **({"picture": picture} if picture else {}),
            }
            user.metadata = metadata
            await maybe_await(self.db.for_tenant(TenantContext(tenant_id=user.tenant_id)).save("users", user))
            return self._build_auth_result(tenant, user)

        tenant_id, slug = await self._unique_google_tenant_identity(normalized_email)
        display_name = full_name.strip() or normalized_email.split("@", 1)[0].replace(".", " ").replace("-", " ").title()
        tenant = await self.tenant_service.create_tenant(
            tenant_id=tenant_id,
            name=display_name or tenant_id,
            slug=slug,
            subscription_plan="Free",
        )
        user = User(
            tenant_id=tenant.tenant_id,
            email=normalized_email,
            full_name=display_name,
            password_hash=hash_password(secrets.token_urlsafe(32)),
            role="member",
            status="active",
            metadata={
                "subscription_plan": "Free",
                "google_auth": {
                    "sub": str(google_sub or ""),
                    "email_verified": True,
                    **({"picture": picture} if picture else {}),
                },
            },
        )
        await maybe_await(self.db.for_tenant(TenantContext(tenant_id=tenant.tenant_id)).save("users", user))
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
        limits = get_plan_limits(tenant.subscription_plan or "Free")
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

    async def _ensure_public_signup_identity_available(
        self,
        requested_tenant_id: str,
        tenant_name: str,
        tenant_slug: str,
    ) -> None:
        requested_values = {
            str(requested_tenant_id or "").strip().lower(),
            str(tenant_name or "").strip().lower(),
            str(tenant_slug or "").strip().lower(),
            self._slugify(tenant_name),
            self._slugify(tenant_slug),
        }
        requested_values.discard("")
        tenants: Sequence[Tenant] = await maybe_await(self.db.tenants.list_all())
        for tenant in tenants:
            existing_values = {
                str(tenant.tenant_id or "").strip().lower(),
                str(tenant.name or "").strip().lower(),
                str(tenant.slug or "").strip().lower(),
                self._slugify(tenant.name),
                self._slugify(tenant.slug),
            }
            existing_values.discard("")
            if requested_values & existing_values:
                raise TenantNameAlreadyTakenError("Tenant name is already taken. Please choose another name.")

    async def _unique_public_signup_slug(self, tenant_name: str) -> str:
        base = self._slugify(tenant_name) or "workspace"
        tenants: Sequence[Tenant] = await maybe_await(self.db.tenants.list_all())
        existing_slugs = {str(tenant.slug or "").strip().lower() for tenant in tenants}
        existing_names = {self._slugify(tenant.name) for tenant in tenants}
        candidate = base
        suffix = 1
        while candidate.lower() in existing_slugs or candidate.lower() in existing_names:
            candidate = f"{base}-{suffix}"
            suffix += 1
        return candidate

    async def _find_user_by_email_globally(self, email: str) -> tuple[Tenant, User] | None:
        normalized = str(email or "").strip().lower()
        users: Sequence[User] = await maybe_await(self.db.users.list_all())
        matches = [user for user in users if user.email.strip().lower() == normalized]
        matches.sort(key=lambda user: (0 if str(user.role or "").strip().lower() == "admin" else 1, user.created_at))
        for user in matches:
            tenant = await self._find_tenant(user.tenant_id)
            if tenant is not None:
                return tenant, user
        return None

    async def _unique_google_tenant_identity(self, email: str) -> tuple[str, str]:
        prefix = str(email or "").split("@", 1)[0]
        base = self._slugify(prefix) or "google-user"
        tenants: Sequence[Tenant] = await maybe_await(self.db.tenants.list_all())
        existing_ids = {str(tenant.tenant_id or "").strip().lower() for tenant in tenants}
        existing_slugs = {str(tenant.slug or "").strip().lower() for tenant in tenants}
        candidate = base
        suffix = 1
        while candidate.lower() in existing_ids or candidate.lower() in existing_slugs:
            candidate = f"{base}-{suffix}"
            suffix += 1
        return candidate, candidate

    def _slugify(self, value: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower())
        slug = re.sub(r"-+", "-", slug).strip("-")
        return slug[:48].strip("-")

    async def _enforce_team_member_limit(self, tenant: Tenant) -> None:
        limits = get_plan_limits(tenant.subscription_plan or "Free")
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
            role=str(user.role or "").strip().lower(),
            plan=tenant.subscription_plan,
            token=token,
            subscription_plan=tenant.subscription_plan,
            usage_limits=get_plan_limits(tenant.subscription_plan),
        )
