"""Utilities for enforcing tenant isolation."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from app.core.models import TenantContext


class TenantIsolationError(ValueError):
    """Raised when an operation crosses tenant boundaries."""


class TenantResolutionError(ValueError):
    """Raised when a request does not provide a valid tenant id."""


CURRENT_TENANT: ContextVar[Optional[TenantContext]] = ContextVar("current_tenant", default=None)


def assert_same_tenant(expected_tenant_id: str, actual_tenant_id: str) -> None:
    if expected_tenant_id != actual_tenant_id:
        raise TenantIsolationError(
            f"Tenant isolation violation: expected tenant_id={expected_tenant_id}, got {actual_tenant_id}."
        )


def context_for_tenant(tenant_id: str, tenant_slug: str = "", user_id: str = "") -> TenantContext:
    return TenantContext(tenant_id=tenant_id, tenant_slug=tenant_slug, user_id=user_id)


def require_tenant_id(tenant_id: str) -> str:
    normalized = str(tenant_id or "").strip()
    if not normalized:
        raise TenantResolutionError("Every request must include a tenant_id.")
    return normalized


def set_current_tenant(tenant: TenantContext):
    return CURRENT_TENANT.set(tenant)


def reset_current_tenant(token: object) -> None:
    CURRENT_TENANT.reset(token)


def get_current_tenant() -> TenantContext:
    tenant = CURRENT_TENANT.get()
    if tenant is None:
        raise TenantResolutionError("Tenant context was not resolved for this request.")
    return tenant


def resolve_tenant_context(
    tenant_id: str = "",
    tenant_slug: str = "",
    user_id: str = "",
    metadata: Optional[Dict[str, Any]] = None,
) -> TenantContext:
    resolved = TenantContext(
        tenant_id=require_tenant_id(tenant_id),
        tenant_slug=str(tenant_slug or "").strip(),
        user_id=str(user_id or "").strip(),
        metadata=dict(metadata or {}),
    )
    return resolved


@dataclass(slots=True)
class TenantMiddleware:
    """Framework-agnostic middleware for tenant resolution."""

    tenant_header: str = "X-Tenant-Id"

    def resolve(self, request: Any) -> TenantContext:
        state = getattr(request, "state", None)
        existing = getattr(state, "tenant", None) if state is not None else None
        if existing is not None:
            return existing
        raise TenantResolutionError("Tenant context must come from authenticated request state.")

    def __call__(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        context = self.resolve(request)
        token = set_current_tenant(context)
        try:
            return handler(request)
        finally:
            reset_current_tenant(token)
