"""Small HTTP hardening middleware."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Awaitable, Callable, Deque, Dict, Tuple

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'")
        return response


class InMemoryRateLimitMiddleware(BaseHTTPMiddleware):
    """Development-friendly limiter. TODO: replace with Redis/shared limiter for multi-worker production."""

    def __init__(self, app, rules: Dict[Tuple[str, str], Tuple[int, int]] | None = None) -> None:
        super().__init__(app)
        self.rules = rules or {
            ("POST", "/login"): (5, 60),
            ("POST", "/signup"): (5, 60),
            ("POST", "/jobs"): (10, 60),
            ("POST", "/jobs/run-once"): (10, 60),
            ("GET", "/leads"): (60, 60),
        }
        self.hits: Dict[Tuple[str, str, str], Deque[float]] = defaultdict(deque)

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        key = (request.method.upper(), request.url.path)
        rule = self.rules.get(key)
        if rule is None:
            return await call_next(request)
        limit, window_seconds = rule
        client_ip = request.client.host if request.client else "unknown"
        bucket_key = (client_ip, key[0], key[1])
        now = time.monotonic()
        bucket = self.hits[bucket_key]
        while bucket and now - bucket[0] > window_seconds:
            bucket.popleft()
        if len(bucket) >= limit:
            return JSONResponse({"detail": "Rate limit exceeded. Please try again shortly."}, status_code=429)
        bucket.append(now)
        return await call_next(request)
