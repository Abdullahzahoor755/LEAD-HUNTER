"""Provider-agnostic AI text generation and tenant provider settings."""

from __future__ import annotations

import json
from typing import Any, Dict

import httpx

from app.core.models import Tenant, TenantContext
from app.db.session import AsyncDatabaseSession, DatabaseSession
from app.services._async import maybe_await
from app.services.security_service import decrypt_secret, encrypt_secret


SUPPORTED_AI_PROVIDERS = {"anthropic", "openai", "gemini", "groq", "openrouter", "fallback"}
DEFAULT_AI_MODELS = {
    "anthropic": "claude-3-5-haiku-latest",
    "openai": "gpt-4o-mini",
    "gemini": "gemini-1.5-flash",
    "groq": "llama-3.1-8b-instant",
    "openrouter": "openai/gpt-4o-mini",
    "fallback": "",
}
OPENAI_COMPATIBLE_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
}


class AIProviderNotConfigured(ValueError):
    """Raised when a tenant has not configured an enabled AI provider."""


class AIProviderService:
    def __init__(self, db: DatabaseSession | AsyncDatabaseSession) -> None:
        self.db = db

    async def save_settings(
        self,
        tenant: TenantContext,
        provider: str,
        api_key: str = "",
        model: str = "",
        enabled: bool = True,
    ) -> Dict[str, Any]:
        normalized_provider = self.normalize_provider(provider)
        tenant_record = await self._get_tenant(tenant.tenant_id)
        settings = dict(tenant_record.settings or {})
        providers = dict(settings.get("providers", {}))
        existing_ai = dict(providers.get("ai", {}))
        api_key_encrypted = str(existing_ai.get("api_key_encrypted", "") or "")
        if normalized_provider == "fallback":
            api_key_encrypted = ""
        elif str(api_key or "").strip():
            api_key_encrypted = encrypt_secret(str(api_key).strip())
        elif not api_key_encrypted and enabled:
            raise AIProviderNotConfigured("API key is required for this AI provider.")
        providers["ai"] = {
            "provider": normalized_provider,
            "model": str(model or DEFAULT_AI_MODELS[normalized_provider]).strip(),
            "api_key_encrypted": api_key_encrypted,
            "enabled": bool(enabled),
        }
        settings["providers"] = providers
        tenant_record.settings = settings
        await maybe_await(self.db.tenants.save(tenant_record))
        return self.public_status(providers["ai"])

    async def status(self, tenant: TenantContext) -> Dict[str, Any]:
        tenant_record = await self._get_tenant(tenant.tenant_id)
        ai_settings = self._raw_ai_settings(tenant_record)
        return self.public_status(ai_settings)

    async def generate_text(
        self,
        tenant: TenantContext,
        system_prompt: str,
        user_prompt: str,
        json_mode: bool = False,
        temperature: float = 0.3,
        max_tokens: int = 1200,
    ) -> dict | str:
        settings = await self._configured_settings(tenant)
        provider = settings["provider"]
        model = settings["model"] or DEFAULT_AI_MODELS[provider]
        api_key = settings["api_key"]
        if provider in OPENAI_COMPATIBLE_BASE_URLS:
            text = await self._generate_openai_compatible(
                provider=provider,
                model=model,
                api_key=api_key,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                json_mode=json_mode,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        elif provider == "anthropic":
            text = await self._generate_anthropic(
                model=model,
                api_key=api_key,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        elif provider == "gemini":
            text = await self._generate_gemini(
                model=model,
                api_key=api_key,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                json_mode=json_mode,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        else:
            raise AIProviderNotConfigured("AI provider is not configured.")
        if json_mode:
            return self._parse_json_response(text)
        return text

    async def test_connection(self, tenant: TenantContext) -> Dict[str, Any]:
        status = await self.status(tenant)
        try:
            result = await self.generate_text(
                tenant,
                system_prompt="You are a connection test. Reply with OK only.",
                user_prompt="Reply with OK only.",
                temperature=0,
                max_tokens=12,
            )
        except AIProviderNotConfigured as error:
            return {**status, "success": False, "message": str(error)}
        except Exception:
            return {**status, "success": False, "message": "AI provider test failed safely."}
        clean_result = str(result or "").strip()
        return {**status, "success": clean_result.upper().startswith("OK"), "message": "AI provider responded."}

    def normalize_provider(self, provider: str) -> str:
        normalized = str(provider or "fallback").strip().lower()
        if normalized not in SUPPORTED_AI_PROVIDERS:
            raise ValueError(f"Unsupported AI provider: {provider}")
        return normalized

    def public_status(self, ai_settings: Dict[str, Any]) -> Dict[str, Any]:
        provider = self.normalize_provider(str(ai_settings.get("provider") or "fallback"))
        enabled = bool(ai_settings.get("enabled", False))
        model = str(ai_settings.get("model") or DEFAULT_AI_MODELS[provider]).strip()
        has_key = bool(str(ai_settings.get("api_key_encrypted", "") or "").strip())
        configured = enabled and (provider == "fallback" or has_key)
        return {
            "configured": configured,
            "provider": provider,
            "model": model,
            "enabled": enabled,
        }

    async def _configured_settings(self, tenant: TenantContext) -> Dict[str, str]:
        tenant_record = await self._get_tenant(tenant.tenant_id)
        ai_settings = self._raw_ai_settings(tenant_record)
        status = self.public_status(ai_settings)
        provider = status["provider"]
        if not status["enabled"] or provider == "fallback":
            raise AIProviderNotConfigured("AI provider is not configured.")
        encrypted_key = str(ai_settings.get("api_key_encrypted", "") or "").strip()
        if not encrypted_key:
            raise AIProviderNotConfigured("AI provider API key is missing.")
        return {
            "provider": provider,
            "model": status["model"],
            "api_key": decrypt_secret(encrypted_key),
        }

    def _raw_ai_settings(self, tenant_record: Tenant) -> Dict[str, Any]:
        providers = dict((tenant_record.settings or {}).get("providers", {}))
        return dict(providers.get("ai", {}))

    async def _get_tenant(self, tenant_id: str) -> Tenant:
        records = await maybe_await(self.db.tenants.list(tenant_id))
        if not records:
            raise ValueError(f"Tenant {tenant_id} not found.")
        tenant = records[0]
        if tenant.tenant_id != tenant_id:
            raise ValueError("Tenant provider lookup mismatch.")
        return tenant

    async def _generate_openai_compatible(
        self,
        provider: str,
        model: str,
        api_key: str,
        system_prompt: str,
        user_prompt: str,
        json_mode: bool,
        temperature: float,
        max_tokens: int,
    ) -> str:
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        if provider == "openrouter":
            headers.update({"HTTP-Referer": "https://lead-hunter-ai.local", "X-Title": "Lead Hunter AI"})
        payload: Dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": float(temperature),
            "max_tokens": int(max_tokens),
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{OPENAI_COMPATIBLE_BASE_URLS[provider]}/chat/completions",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
        return str(data.get("choices", [{}])[0].get("message", {}).get("content", "") or "").strip()

    async def _generate_anthropic(
        self,
        model: str,
        api_key: str,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        max_tokens: int,
    ) -> str:
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload = {
            "model": model,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
            "temperature": float(temperature),
            "max_tokens": int(max_tokens),
        }
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post("https://api.anthropic.com/v1/messages", headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
        content = data.get("content", [])
        if content and isinstance(content[0], dict):
            return str(content[0].get("text", "") or "").strip()
        return ""

    async def _generate_gemini(
        self,
        model: str,
        api_key: str,
        system_prompt: str,
        user_prompt: str,
        json_mode: bool,
        temperature: float,
        max_tokens: int,
    ) -> str:
        payload: Dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {
                "temperature": float(temperature),
                "maxOutputTokens": int(max_tokens),
            },
        }
        if json_mode:
            payload["generationConfig"]["responseMimeType"] = "application/json"
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                params={"key": api_key},
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
        candidates = data.get("candidates", [])
        if not candidates:
            return ""
        parts = candidates[0].get("content", {}).get("parts", [])
        if parts and isinstance(parts[0], dict):
            return str(parts[0].get("text", "") or "").strip()
        return ""

    def _parse_json_response(self, text: str) -> dict:
        raw = str(text or "").strip()
        if raw.startswith("```"):
            raw = raw.strip("`").strip()
            if raw.lower().startswith("json"):
                raw = raw[4:].strip()
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("AI provider returned non-object JSON.")
        return parsed
