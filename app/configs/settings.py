"""Application settings for the multi-tenant SaaS runtime."""

from __future__ import annotations

import os
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Dict


def Field(default: str = "", alias: str = ""):
    """Small env-alias helper for this dataclass settings module."""

    return dataclass_field(default_factory=lambda: os.getenv(alias, default))


@dataclass(slots=True)
class Settings:
    app_name: str = "Lead Generator SaaS"
    environment: str = dataclass_field(default_factory=lambda: os.getenv("APP_ENV", "development"))
    database_backend: str = dataclass_field(default_factory=lambda: os.getenv("DATABASE_BACKEND", "postgres"))
    database_url: str = dataclass_field(
        default_factory=lambda: os.getenv(
            "DATABASE_URL",
            "postgresql+asyncpg://postgres:postgres@localhost:5432/lead_generator",
        )
    )
    database_echo: bool = dataclass_field(default_factory=lambda: os.getenv("DATABASE_ECHO", "false").lower() == "true")
    jwt_secret: str = dataclass_field(default_factory=lambda: os.getenv("JWT_SECRET", "dev-secret-change-me"))
    secret_encryption_key: str = dataclass_field(default_factory=lambda: os.getenv("SECRET_ENCRYPTION_KEY", ""))
    jwt_expiration_seconds: int = dataclass_field(default_factory=lambda: int(os.getenv("JWT_EXPIRATION_SECONDS", "86400")))
    default_queue: str = dataclass_field(default_factory=lambda: os.getenv("JOB_QUEUE", "default"))
    data_dir: Path = dataclass_field(default_factory=lambda: Path(os.getenv("DATA_DIR", "data")))
    enable_legacy_runtime: bool = dataclass_field(
        default_factory=lambda: os.getenv("ENABLE_LEGACY_RUNTIME", "false").lower() == "true"
    )
    provider_config: Dict[str, str] = dataclass_field(
        default_factory=lambda: {
            "anthropic_model": os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
        }
    )
    billing_plan_prices: Dict[str, int] = dataclass_field(
        default_factory=lambda: {
            "Starter": int(os.getenv("BILLING_PRICE_STARTER_PKR", "5000")),
            "Pro": int(os.getenv("BILLING_PRICE_PRO_PKR", "15000")),
            "Agency": int(os.getenv("BILLING_PRICE_AGENCY_PKR", "45000")),
        }
    )
    billing_nayapay_name: str = dataclass_field(default_factory=lambda: os.getenv("BILLING_NAYAPAY_NAME", "Muhammad Abdullah"))
    billing_nayapay_account: str = dataclass_field(default_factory=lambda: os.getenv("BILLING_NAYAPAY_ACCOUNT", "mian755@nayapay"))
    billing_sadapay_name: str = dataclass_field(default_factory=lambda: os.getenv("BILLING_SADAPAY_NAME", "Muhammad Abdullah"))
    billing_sadapay_account: str = dataclass_field(default_factory=lambda: os.getenv("BILLING_SADAPAY_ACCOUNT", "mian755@sadapay"))
    billing_qr_code_url: str = dataclass_field(default_factory=lambda: os.getenv("BILLING_QR_CODE_URL", ""))
    google_oauth_client_id: str = Field(default="", alias="GOOGLE_OAUTH_CLIENT_ID")
    google_oauth_client_secret: str = Field(default="", alias="GOOGLE_OAUTH_CLIENT_SECRET")
    google_oauth_redirect_uri: str = Field(default="", alias="GOOGLE_OAUTH_REDIRECT_URI")
    frontend_base_url: str = dataclass_field(
        default_factory=lambda: os.getenv("FRONTEND_BASE_URL", os.getenv("APP_FRONTEND_URL", ""))
    )


settings = Settings()
