"""Provider interfaces and shared payload types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Protocol


@dataclass(slots=True)
class ProviderSendRequest:
    to: str
    subject: str = ""
    body: str = ""
    thread_id: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ProviderSendResult:
    message_id: str = ""
    thread_id: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ProviderReply:
    from_address: str = ""
    subject: str = ""
    body: str = ""
    message_id: str = ""
    thread_id: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ProviderAccount:
    tenant_id: str
    provider: str = "gmail"
    access_token: str = ""
    refresh_token: str = ""
    client_id: str = ""
    client_secret: str = ""
    token_uri: str = "https://oauth2.googleapis.com/token"
    scopes: List[str] = field(default_factory=list)
    email_address: str = ""
    expiry: str = ""


class MessageProvider(Protocol):
    async def send(self, account: ProviderAccount, request: ProviderSendRequest) -> ProviderSendResult:
        ...

    async def fetch_replies(self, account: ProviderAccount, cursor: str = "") -> List[ProviderReply]:
        ...
