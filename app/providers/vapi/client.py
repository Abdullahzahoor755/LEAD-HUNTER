"""Vapi client foundation for voice calling."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict

import httpx

from app.configs.settings import settings


class VapiConfigurationError(RuntimeError):
    """Raised when Vapi credentials are missing."""


class VapiCallError(RuntimeError):
    """Raised when Vapi rejects or cannot create a call."""


@dataclass(slots=True)
class VapiClient:
    api_key: str = ""
    assistant_id: str = ""
    base_url: str = ""

    def __post_init__(self) -> None:
        if not self.api_key:
            self.api_key = str(settings.vapi_api_key or "").strip()
        if not self.assistant_id:
            self.assistant_id = str(settings.vapi_assistant_id or "").strip()
        if not self.base_url:
            self.base_url = str(settings.vapi_base_url or "https://api.vapi.ai").strip().rstrip("/")

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
        payload: Dict[str, Any] = {
            "assistantId": self.assistant_id,
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
            raise VapiCallError(f"Vapi call request failed with status {response.status_code}.")
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
