"""Vapi client foundation for voice calling."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Dict

import httpx

from app.configs import settings as settings_module


class VapiConfigurationError(RuntimeError):
    """Raised when Vapi credentials are missing."""


class VapiCallError(RuntimeError):
    """Raised when Vapi rejects or cannot create a call."""


class VapiProviderError(VapiCallError):
    """Raised with safe diagnostics when Vapi returns an error response."""

    def __init__(self, status_code: int, safe_message: str, provider_code: str = "") -> None:
        self.status_code = status_code
        self.safe_message = safe_message
        self.provider_code = provider_code
        super().__init__(safe_message)


BEARER_PATTERN = re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
KEY_VALUE_SECRET_PATTERN = re.compile(r"(api[_-]?key|token|secret|authorization)(['\"\s:=]+)([A-Za-z0-9._~+/=-]{8,})", re.IGNORECASE)


def safe_provider_error(response: httpx.Response) -> tuple[str, str]:
    provider_code = ""
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if isinstance(payload, dict):
        provider_code = str(payload.get("code") or payload.get("errorCode") or payload.get("error_code") or "").strip()
        message = payload.get("message") or payload.get("error") or payload.get("detail") or payload
        if isinstance(message, (dict, list)):
            raw_message = json.dumps(message, default=str)
        else:
            raw_message = str(message or "")
    else:
        raw_message = str(response.text or "")
    return _redact_provider_text(raw_message)[:300], _redact_provider_text(provider_code)[:80]


def _redact_provider_text(value: str) -> str:
    redacted = str(value or "")
    redacted = BEARER_PATTERN.sub("Bearer [redacted]", redacted)
    redacted = KEY_VALUE_SECRET_PATTERN.sub(lambda match: f"{match.group(1)}{match.group(2)}[redacted]", redacted)
    api_key = str(settings_module.settings.vapi_api_key or "")
    return redacted.replace(api_key, "[redacted]") if api_key else redacted


@dataclass(slots=True)
class VapiClient:
    api_key: str = ""
    assistant_id: str = ""
    base_url: str = ""

    def __post_init__(self) -> None:
        if not self.api_key:
            self.api_key = str(settings_module.settings.vapi_api_key or "").strip()
        if not self.assistant_id:
            self.assistant_id = str(settings_module.settings.vapi_assistant_id or "").strip()
        if not self.base_url:
            self.base_url = str(settings_module.settings.vapi_base_url or "https://api.vapi.ai").strip().rstrip("/")

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.assistant_id and self.base_url)

    def status(self) -> Dict[str, Any]:
        return {
            "configured": self.configured,
            "api_key_present": bool(self.api_key),
            "assistant_id_present": bool(self.assistant_id),
        }

    async def provider_reachable(self) -> bool:
        if not self.configured:
            return False
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(
                    f"{self.base_url}/assistant/{self.assistant_id}",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
        except httpx.HTTPError:
            return False
        return response.status_code < 500

    async def create_call(self, *, phone_number: str, lead_id: str = "", metadata: Dict[str, Any] | None = None) -> Dict[str, Any]:
        if not self.configured:
            raise VapiConfigurationError("Vapi is not configured. Set VAPI_API_KEY and VAPI_ASSISTANT_ID before creating calls.")
        clean_phone = str(phone_number or "").strip()
        if not clean_phone:
            raise VapiCallError("A phone number is required to create a Vapi call.")
        phone_number_id = str(settings_module.settings.vapi_phone_number_id or "").strip()
        if not phone_number_id:
            raise VapiProviderError(0, "VAPI_PHONE_NUMBER_ID not configured")
        payload: Dict[str, Any] = {
            "assistantId": self.assistant_id,
            "phoneNumberId": phone_number_id,
            "customer": {"number": clean_phone},
        }
        request_metadata = dict(metadata or {})
        if lead_id:
            request_metadata.setdefault("lead_id", lead_id)
        if request_metadata:
            payload["metadata"] = request_metadata
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post(
                    f"{self.base_url}/call",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
        except httpx.HTTPError as error:
            raise VapiCallError("Vapi call request failed safely.") from error
        if response.status_code >= 400:
            safe_message, provider_code = safe_provider_error(response)
            if not safe_message:
                safe_message = f"Vapi call request failed with status {response.status_code}."
            raise VapiProviderError(response.status_code, safe_message, provider_code)
        try:
            data = response.json()
        except ValueError as error:
            raise VapiCallError("Vapi returned an invalid response.") from error
        if not isinstance(data, dict):
            raise VapiCallError("Vapi returned an invalid response.")
        return data

    async def get_call_status(self, *_: Any, **__: Any) -> Dict[str, Any]:
        if not self.configured:
            raise VapiConfigurationError("Vapi is not configured. Set VAPI_API_KEY and VAPI_ASSISTANT_ID before checking call status.")
        raise RuntimeError("Voice calling foundation added; implementation pending.")
