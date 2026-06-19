"""Safe parsing helpers for Vapi webhooks."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict


@dataclass(slots=True)
class VapiWebhookEvent:
    event_type: str = ""
    provider_call_id: str = ""
    transcript: str = ""
    duration_seconds: int = 0
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def ignored(self) -> bool:
        return not self.event_type or not self.provider_call_id


def parse_vapi_webhook(payload: Any) -> VapiWebhookEvent:
    if not isinstance(payload, dict):
        return VapiWebhookEvent()
    event_type = _first_text(payload, ("type", "event", "message.type")).strip().lower()
    provider_call_id = _first_text(payload, ("call.id", "message.call.id", "callId", "id")).strip()
    transcript = _extract_transcript(payload)
    duration_seconds = _extract_duration_seconds(payload)
    return VapiWebhookEvent(
        event_type=event_type,
        provider_call_id=provider_call_id,
        transcript=transcript,
        duration_seconds=duration_seconds,
        raw=payload,
    )


def _first_text(payload: Dict[str, Any], paths: tuple[str, ...]) -> str:
    for path in paths:
        value = _path_value(payload, path)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, (int, float)):
            return str(value)
    return ""


def _path_value(payload: Dict[str, Any], path: str) -> Any:
    value: Any = payload
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _extract_transcript(payload: Dict[str, Any]) -> str:
    for path in ("transcript", "call.transcript", "message.transcript"):
        value = _path_value(payload, path)
        if isinstance(value, str) and value.strip():
            return value.strip()
    messages = _path_value(payload, "messages")
    if messages is None:
        messages = _path_value(payload, "message.messages")
    if isinstance(messages, list):
        return _messages_to_transcript(messages)
    return ""


def _messages_to_transcript(messages: list[Any]) -> str:
    lines: list[str] = []
    for item in messages:
        if not isinstance(item, dict):
            continue
        text = str(item.get("message") or item.get("content") or item.get("text") or "").strip()
        if not text:
            continue
        role = str(item.get("role") or item.get("speaker") or "").strip()
        lines.append(f"{role}: {text}" if role else text)
    return "\n".join(lines).strip()


def _extract_duration_seconds(payload: Dict[str, Any]) -> int:
    for path in (
        "durationSeconds",
        "duration_seconds",
        "duration",
        "call.durationSeconds",
        "call.duration_seconds",
        "call.duration",
        "message.durationSeconds",
        "message.duration_seconds",
        "message.duration",
    ):
        value = _path_value(payload, path)
        if isinstance(value, (int, float)) and value >= 0:
            return int(value)
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    return 0
