"""Temporary outreach audit logging helpers."""

from __future__ import annotations

import logging
import os

from app.configs.settings import settings


def audit_enabled() -> bool:
    environment = str(os.getenv("APP_ENV") or os.getenv("ENV") or settings.environment or "development").strip().lower()
    debug_enabled = str(os.getenv("DEBUG", "")).strip().lower() in {"1", "true", "yes", "on"}
    return debug_enabled or environment == "development"


def audit_log(logger: logging.Logger, level: int, message: str, *args: object, exc_info: bool = False) -> None:
    if audit_enabled():
        logger.log(level, message, *args, exc_info=exc_info)


def production_error_log(logger: logging.Logger, message: str, *args: object) -> None:
    logger.warning(message, *args)
