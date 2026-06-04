"""Framework-agnostic authentication middleware helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict

from app.core.auth import decode_jwt_token
from app.core.models import TenantContext
from app.core.tenant import reset_current_tenant, resolve_tenant_context, set_current_tenant


class AuthenticationMiddlewareError(ValueError):
    """Raised when the auth middleware cannot resolve a valid bearer token."""


@dataclass(slots=True)
class AuthMiddleware:
    authorization_header: str = "Authorization"

    def resolve(self, request: Any) -> Dict[str, Any]:
        headers = getattr(request, "headers", {}) or {}
        raw_header = headers.get(self.authorization_header, "") or headers.get(self.authorization_header.lower(), "")
        if not raw_header.startswith("Bearer "):
            raise AuthenticationMiddlewareError("Missing bearer token.")
        token = raw_header.split(" ", 1)[1].strip()
        payload = decode_jwt_token(token)
        tenant = resolve_tenant_context(
            tenant_id=str(payload.get("tenant_id", "")),
            tenant_slug=str(payload.get("tenant_slug", "")),
            user_id=str(payload.get("user_id", "")),
            metadata={"email": str(payload.get("email", "")), "role": str(payload.get("role", ""))},
        )
        state = getattr(request, "state", None)
        if state is not None:
            setattr(state, "tenant", tenant)
            setattr(state, "auth", payload)
        return payload

    def __call__(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        payload = self.resolve(request)
        tenant = TenantContext(
            tenant_id=str(payload.get("tenant_id", "")),
            tenant_slug=str(payload.get("tenant_slug", "")),
            user_id=str(payload.get("user_id", "")),
            metadata={"email": str(payload.get("email", "")), "role": str(payload.get("role", ""))},
        )
        token = set_current_tenant(tenant)
        try:
            return handler(request)
        finally:
            reset_current_tenant(token)
