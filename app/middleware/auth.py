"""FastAPI middleware that resolves auth and tenant context per request."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.auth import decode_jwt_token
from app.core.models import TenantContext
from app.core.tenant import reset_current_tenant, set_current_tenant


class AuthTenantMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        public_paths = {
            "/",
            "/app",
            "/privacy",
            "/terms",
            "/contact",
            "/gmail-access",
            "/public/homepage.css",
            "/healthz",
            "/readyz",
            "/signup",
            "/login",
            "/auth/google/start",
            "/auth/google/callback",
            "/settings/providers/gmail/oauth/callback",
            "/voice/webhook/vapi",
        }
        if request.url.path in public_paths:
            return await call_next(request)

        raw_header = request.headers.get("authorization", "")
        if not raw_header.startswith("Bearer "):
            return JSONResponse({"detail": "Missing bearer token."}, status_code=401)

        try:
            token = raw_header.split(" ", 1)[1].strip()
            payload = decode_jwt_token(token)
        except ValueError as error:
            return JSONResponse({"detail": str(error)}, status_code=401)
        tenant = TenantContext(
            tenant_id=str(payload.get("tenant_id", "")).strip(),
            tenant_slug=str(payload.get("tenant_slug", "")).strip(),
            user_id=str(payload.get("user_id", "")).strip(),
            metadata={"email": str(payload.get("email", "")), "role": str(payload.get("role", ""))},
        )
        if not tenant.tenant_id or not tenant.user_id:
            return JSONResponse({"detail": "Invalid authentication token."}, status_code=401)

        request.state.user = payload
        request.state.tenant = tenant
        request.state.tenant_id = tenant.tenant_id
        token_state = set_current_tenant(tenant)
        try:
            return await call_next(request)
        finally:
            reset_current_tenant(token_state)
