"""Authentication and subscription helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any, Dict
from urllib.parse import urlparse

from app.configs.settings import settings

DEFAULT_JWT_SECRET = "dev-secret-change-me"

PLAN_LIMITS: Dict[str, Dict[str, int]] = {
    "Free": {
        "monthly_leads": 250,
        "monthly_emails": 0,
        "monthly_reply_checks": 0,
        "team_members": 1,
    },
    "Starter": {
        "monthly_leads": 250,
        "monthly_emails": 500,
        "monthly_reply_checks": 1000,
        "team_members": 1,
    },
    "Pro": {
        "monthly_leads": 2500,
        "monthly_emails": 5000,
        "monthly_reply_checks": 10000,
        "team_members": 5,
    },
    "Agency": {
        "monthly_leads": 25000,
        "monthly_emails": 50000,
        "monthly_reply_checks": 100000,
        "team_members": 25,
    },
}


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def hash_password(password: str, salt: str | None = None) -> str:
    resolved_salt = salt or secrets.token_hex(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), resolved_salt.encode("utf-8"), 120_000)
    return f"pbkdf2_sha256${resolved_salt}${derived.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        algorithm, salt, digest = password_hash.split("$", 2)
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    candidate = hash_password(password, salt=salt)
    return hmac.compare_digest(candidate, password_hash)


def get_jwt_secret() -> str:
    return settings.jwt_secret or os.getenv("JWT_SECRET", DEFAULT_JWT_SECRET)


def validate_production_jwt_secret() -> None:
    environment = str(os.getenv("APP_ENV") or os.getenv("ENV") or settings.environment or "development").strip().lower()
    secret = str(os.getenv("JWT_SECRET") or settings.jwt_secret or "").strip()
    if environment == "production" and (not secret or secret == DEFAULT_JWT_SECRET):
        raise RuntimeError("JWT_SECRET must be set to a non-default value in production.")
    if environment != "production":
        return
    encryption_key = str(os.getenv("SECRET_ENCRYPTION_KEY") or settings.secret_encryption_key or "").strip()
    if not encryption_key:
        raise RuntimeError("SECRET_ENCRYPTION_KEY must be set in production.")
    parsed_db_url = urlparse(str(os.getenv("DATABASE_URL") or settings.database_url or ""))
    if parsed_db_url.password in {"postgres", "password", "admin", "root", ""}:
        raise RuntimeError("DATABASE_URL must not use a default database password in production.")
    debug_enabled = str(os.getenv("DEBUG", "")).strip().lower() in {"1", "true", "yes", "on"}
    if debug_enabled:
        raise RuntimeError("DEBUG must be disabled in production.")


def create_jwt_token(payload: Dict[str, Any], expires_in_seconds: int | None = None) -> str:
    now = int(time.time())
    ttl = expires_in_seconds or settings.jwt_expiration_seconds
    header = {"alg": "HS256", "typ": "JWT"}
    body = dict(payload)
    body.setdefault("iat", now)
    body.setdefault("exp", now + ttl)
    header_b64 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    body_b64 = _b64url_encode(json.dumps(body, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header_b64}.{body_b64}".encode("ascii")
    signature = hmac.new(get_jwt_secret().encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{header_b64}.{body_b64}.{_b64url_encode(signature)}"


def decode_jwt_token(token: str) -> Dict[str, Any]:
    try:
        header_b64, body_b64, signature_b64 = token.split(".")
    except ValueError as error:
        raise ValueError("Invalid JWT format.") from error
    signing_input = f"{header_b64}.{body_b64}".encode("ascii")
    expected_signature = hmac.new(get_jwt_secret().encode("utf-8"), signing_input, hashlib.sha256).digest()
    if not hmac.compare_digest(expected_signature, _b64url_decode(signature_b64)):
        raise ValueError("Invalid JWT signature.")
    payload = json.loads(_b64url_decode(body_b64).decode("utf-8"))
    if int(payload.get("exp", 0) or 0) < int(time.time()):
        raise ValueError("JWT token has expired.")
    return payload


def normalize_subscription_plan(plan_name: str) -> str:
    normalized = str(plan_name or "").strip().title()
    if normalized not in PLAN_LIMITS:
        raise ValueError(f"Unsupported subscription plan: {plan_name}")
    return normalized


def get_plan_limits(plan_name: str) -> Dict[str, int]:
    return dict(PLAN_LIMITS[normalize_subscription_plan(plan_name)])


PRO_FEATURE_PLANS = {"Pro", "Agency"}
GATED_AGENT_NAMES = {"outreach", "reply_monitor", "followup"}


def has_pro_features(plan_name: str) -> bool:
    try:
        return normalize_subscription_plan(plan_name) in PRO_FEATURE_PLANS
    except ValueError:
        return False


def is_plan_gated_agent(agent_name: str) -> bool:
    return str(agent_name or "").strip().lower() in GATED_AGENT_NAMES
