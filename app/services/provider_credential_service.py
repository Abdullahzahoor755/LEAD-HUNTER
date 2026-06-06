"""Tenant-scoped provider credential persistence."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

from app.configs.settings import settings
from app.core.models import Tenant, TenantContext
from app.db.session import AsyncDatabaseSession, DatabaseSession
from app.services._async import maybe_await
from app.services.security_service import decrypt_secret, encrypt_secret


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
            return {}
        if provider_settings.get("connected") is False:
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
        return decrypted

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
