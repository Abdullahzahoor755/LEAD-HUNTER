"""Tenant-scoped provider credential persistence."""

from __future__ import annotations

from typing import Any, Dict

from app.core.models import Tenant, TenantContext
from app.db.session import AsyncDatabaseSession, DatabaseSession
from app.services._async import maybe_await
from app.services.security_service import decrypt_secret, encrypt_secret


class ProviderCredentialService:
    def __init__(self, db: DatabaseSession | AsyncDatabaseSession) -> None:
        self.db = db

    async def get_gmail_credentials(self, tenant: TenantContext) -> Dict[str, Any]:
        tenant_record = await self._get_tenant(tenant.tenant_id)
        provider_settings = dict((tenant_record.settings or {}).get("providers", {}).get("gmail", {}))
        if not provider_settings:
            return {}
        decrypted = dict(provider_settings)
        encrypted_refresh = str(provider_settings.get("refresh_token_encrypted", "")).strip()
        if encrypted_refresh:
            decrypted["refresh_token"] = decrypt_secret(encrypted_refresh)
        decrypted.pop("refresh_token_encrypted", None)
        return decrypted

    async def save_gmail_credentials(self, tenant: TenantContext, credentials: Dict[str, Any]) -> Tenant:
        tenant_record = await self._get_tenant(tenant.tenant_id)
        provider_settings = dict((tenant_record.settings or {}).get("providers", {}))
        gmail_settings = {
            "provider": "gmail",
            "client_id": str(credentials.get("client_id", "")),
            "client_secret": str(credentials.get("client_secret", "")),
            "token_uri": str(credentials.get("token_uri", "https://oauth2.googleapis.com/token")),
            "scopes": list(credentials.get("scopes", [])),
            "refresh_token_encrypted": encrypt_secret(str(credentials.get("refresh_token", ""))),
            "access_token": str(credentials.get("access_token", "")),
            "expiry": str(credentials.get("expiry", "")),
            "email_address": str(credentials.get("email_address", "")).strip().lower(),
        }
        provider_settings["gmail"] = gmail_settings
        tenant_record.settings["providers"] = provider_settings
        return await maybe_await(self.db.tenants.save(tenant_record))

    async def _get_tenant(self, tenant_id: str) -> Tenant:
        records = await maybe_await(self.db.tenants.list(tenant_id))
        if not records:
            raise ValueError(f"Tenant {tenant_id} not found.")
        tenant = records[0]
        if tenant.tenant_id != tenant_id:
            raise ValueError("Tenant provider lookup mismatch.")
        return tenant
