"""Tenant-scoped provider credential persistence."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Any, Dict

from app.configs.settings import settings
from app.core.models import Tenant, TenantContext
from app.providers.base import ProviderAccount
from app.db.session import AsyncDatabaseSession, DatabaseSession
from app.services._async import maybe_await
from app.services.outreach_audit import audit_log
from app.services.security_service import decrypt_secret, encrypt_secret


LOGGER = logging.getLogger(__name__)


def _settings_value(attribute: str, env_name: str, default: str = "") -> str:
    import os

    return str(getattr(settings, attribute, "") or os.getenv(env_name, default) or "").strip()


def _google_oauth_client_id() -> str:
    return _settings_value("google_oauth_client_id", "GOOGLE_OAUTH_CLIENT_ID")


def _google_oauth_client_secret() -> str:
    return _settings_value("google_oauth_client_secret", "GOOGLE_OAUTH_CLIENT_SECRET")


class ProviderCredentialService:
    def __init__(self, db: DatabaseSession | AsyncDatabaseSession) -> None:
        self.db = db

    async def get_gmail_credentials(self, tenant: TenantContext) -> Dict[str, Any]:
        tenant_record = await self._get_tenant(tenant.tenant_id)
        provider_settings = dict((tenant_record.settings or {}).get("providers", {}).get("gmail", {}))
        if not provider_settings:
            audit_log(LOGGER, logging.INFO, "OUTREACH_AUDIT gmail.credentials_missing tenant_id=%s reason=no_provider_settings", tenant.tenant_id)
            return {}
        if provider_settings.get("connected") is False:
            audit_log(LOGGER, logging.INFO, "OUTREACH_AUDIT gmail.credentials_missing tenant_id=%s reason=disconnected", tenant.tenant_id)
            return {}
        decrypted: Dict[str, Any] = {
            "provider": "gmail",
            "client_id": str(_google_oauth_client_id() or provider_settings.get("client_id", "")).strip(),
            "client_secret": "",
            "token_uri": str(provider_settings.get("token_uri", "https://oauth2.googleapis.com/token")),
            "scopes": list(provider_settings.get("scopes", [])),
            "access_token": str(provider_settings.get("access_token", "") or ""),
            "expiry": str(provider_settings.get("expiry", "") or ""),
            "email_address": str(
                provider_settings.get("email_address") or provider_settings.get("sender_email") or ""
            ).strip().lower(),
        }
        encrypted_secret = str(provider_settings.get("client_secret_encrypted", "")).strip()
        google_client_secret = _google_oauth_client_secret()
        if google_client_secret:
            decrypted["client_secret"] = google_client_secret
        elif encrypted_secret:
            decrypted["client_secret"] = decrypt_secret(encrypted_secret)
        else:
            decrypted["client_secret"] = str(provider_settings.get("client_secret", "") or "")
        encrypted_refresh = str(provider_settings.get("refresh_token_encrypted", "")).strip()
        if encrypted_refresh:
            decrypted["refresh_token"] = decrypt_secret(encrypted_refresh)
        elif provider_settings.get("refresh_token"):
            decrypted["refresh_token"] = str(provider_settings.get("refresh_token") or "")
        else:
            decrypted["refresh_token"] = ""
        audit_log(
            LOGGER,
            logging.INFO,
            "OUTREACH_AUDIT gmail.credentials_loaded tenant_id=%s sender_email=%s has_client_id=%s has_client_secret=%s has_refresh_token=%s has_access_token=%s connected=%s",
            tenant.tenant_id,
            decrypted["email_address"],
            bool(decrypted["client_id"]),
            bool(decrypted["client_secret"]),
            bool(decrypted["refresh_token"]),
            bool(decrypted["access_token"]),
            provider_settings.get("connected", True),
        )
        return decrypted

    async def gmail_connection_health(self, tenant: TenantContext, provider: Any | None = None) -> Dict[str, Any]:
        credentials = await self.get_gmail_credentials(tenant)
        last_successful_send = await self._last_successful_gmail_send(tenant)
        base = {
            "configured": bool(credentials),
            "connected": False,
            "status": "missing_credentials",
            "status_label": "Missing credentials",
            "sender_email": str(credentials.get("email_address", "") if credentials else "").strip().lower(),
            "last_successful_send": last_successful_send,
            "error": "gmail_not_connected",
        }
        if not credentials:
            return base
        has_token = bool(str(credentials.get("refresh_token", "") or credentials.get("access_token", "") or "").strip())
        has_sender = bool(str(credentials.get("email_address", "") or credentials.get("sender_email", "") or "").strip())
        has_client = bool(str(credentials.get("client_id", "") or "").strip() and str(credentials.get("client_secret", "") or "").strip())
        if not has_token:
            return {**base, "status": "invalid_credentials", "status_label": "Invalid credentials", "error": "gmail_missing_refresh_token"}
        if not has_sender or not has_client:
            return {**base, "status": "invalid_credentials", "status_label": "Invalid credentials", "error": "gmail_sender_not_verified"}
        if provider is not None:
            try:
                account = ProviderAccount(tenant_id=tenant.tenant_id, **credentials)
                raw = await provider.health_check(account)
                sender = str(raw.get("email_address", "") or base["sender_email"]).strip().lower() if isinstance(raw, dict) else base["sender_email"]
                return {
                    **base,
                    "connected": True,
                    "status": "connected",
                    "status_label": "Connected",
                    "sender_email": sender,
                    "error": "",
                }
            except Exception as error:
                reason = self.classify_gmail_health_error(error)
                return {
                    **base,
                    "status": "gmail_api_disabled" if reason == "gmail_api_disabled" else "invalid_credentials",
                    "status_label": "Gmail API disabled" if reason == "gmail_api_disabled" else "Invalid credentials",
                    "error": reason,
                }
        return {
            **base,
            "connected": True,
            "status": "connected",
            "status_label": "Connected",
            "error": "",
        }

    def classify_gmail_health_error(self, error: Exception) -> str:
        text = f"{type(error).__name__} {error}".lower()
        if any(marker in text for marker in ("accessnotconfigured", "api has not been used", "gmail api", "disabled")):
            return "gmail_api_disabled"
        if any(marker in text for marker in ("invalid_grant", "refresh token", "refresherror")):
            return "gmail_refresh_failed"
        if "insufficient" in text or "insufficientpermissions" in text or "insufficient scopes" in text:
            return "gmail_insufficient_scopes"
        if any(marker in text for marker in ("oauth", "token", "unauthorized", "401")):
            return "gmail_token_expired"
        return "gmail_unknown_send_error"

    async def _last_successful_gmail_send(self, tenant: TenantContext) -> str:
        emails = await maybe_await(self.db.for_tenant(tenant).list("emails"))
        sent = [
            item
            for item in emails
            if str(item.provider or "").strip().lower() == "gmail"
            and str(item.direction or "").strip().lower() == "outbound"
            and str(item.status or "").strip().lower() == "sent"
            and item.sent_at is not None
        ]
        if not sent:
            return ""
        latest = sorted(sent, key=lambda item: item.sent_at, reverse=True)[0]
        return latest.sent_at.isoformat() if hasattr(latest.sent_at, "isoformat") else str(latest.sent_at or "")

    async def save_gmail_credentials(self, tenant: TenantContext, credentials: Dict[str, Any]) -> Tenant:
        """Save legacy/manual Gmail credentials while storing secrets encrypted."""

        tenant_record = await self._get_tenant(tenant.tenant_id)
        settings_payload = dict(tenant_record.settings or {})
        provider_settings = dict(settings_payload.get("providers", {}))
        client_secret = str(credentials.get("client_secret", "") or "")
        client_secret_encrypted = encrypt_secret(client_secret) if client_secret else ""
        refresh_token = str(credentials.get("refresh_token", "") or "")
        gmail_settings = {
            "provider": "gmail",
            "client_id": str(credentials.get("client_id", "")),
            "token_uri": str(credentials.get("token_uri", "https://oauth2.googleapis.com/token")),
            "scopes": list(credentials.get("scopes", [])),
            "refresh_token_encrypted": encrypt_secret(refresh_token),
            "access_token": str(credentials.get("access_token", "")),
            "expiry": str(credentials.get("expiry", "")),
            "email_address": str(credentials.get("email_address", "")).strip().lower(),
            "connected": bool(refresh_token or credentials.get("access_token")),
            "connected_at": datetime.now(timezone.utc).isoformat(),
        }
        if client_secret_encrypted:
            gmail_settings["client_secret_encrypted"] = client_secret_encrypted
        provider_settings["gmail"] = gmail_settings
        settings_payload["providers"] = provider_settings
        tenant_record.settings = settings_payload
        return await maybe_await(self.db.tenants.save(tenant_record))

    async def save_gmail_oauth_credentials(self, tenant: TenantContext, credentials: Dict[str, Any]) -> Tenant:
        tenant_record = await self._get_tenant(tenant.tenant_id)
        settings_payload = dict(tenant_record.settings or {})
        provider_settings = dict(settings_payload.get("providers", {}))
        refresh_token = str(credentials.get("refresh_token", "") or "").strip()
        client_secret = str(credentials.get("client_secret", "") or "").strip()
        gmail_settings: Dict[str, Any] = {
            "provider": "gmail",
            "client_id": str(credentials.get("client_id") or _google_oauth_client_id() or "").strip(),
            "token_uri": str(credentials.get("token_uri", "https://oauth2.googleapis.com/token")),
            "scopes": list(credentials.get("scopes", [])),
            "refresh_token_encrypted": encrypt_secret(refresh_token),
            "access_token": str(credentials.get("access_token", "") or ""),
            "expiry": str(credentials.get("expiry", "") or ""),
            "email_address": str(credentials.get("email_address", "") or "").strip().lower(),
            "connected": True,
            "connected_at": datetime.now(timezone.utc).isoformat(),
        }
        if client_secret and not _google_oauth_client_secret():
            gmail_settings["client_secret_encrypted"] = encrypt_secret(client_secret)
        provider_settings["gmail"] = gmail_settings
        settings_payload["providers"] = provider_settings
        tenant_record.settings = settings_payload
        return await maybe_await(self.db.tenants.save(tenant_record))

    async def disconnect_gmail(self, tenant: TenantContext) -> Tenant:
        tenant_record = await self._get_tenant(tenant.tenant_id)
        settings_payload = dict(tenant_record.settings or {})
        provider_settings = dict(settings_payload.get("providers", {}))
        provider_settings.pop("gmail", None)
        settings_payload["providers"] = provider_settings
        tenant_record.settings = settings_payload
        return await maybe_await(self.db.tenants.save(tenant_record))

    async def _get_tenant(self, tenant_id: str) -> Tenant:
        records = await maybe_await(self.db.tenants.list(tenant_id))
        if not records:
            raise ValueError(f"Tenant {tenant_id} not found.")
        tenant = records[0]
        if tenant.tenant_id != tenant_id:
            raise ValueError("Tenant provider lookup mismatch.")
        return tenant
