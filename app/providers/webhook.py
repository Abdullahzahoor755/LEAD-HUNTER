"""Config-driven provider adapters for non-Gmail channels."""

from __future__ import annotations

from typing import List

import httpx

from app.providers.base import MessageProvider, ProviderAccount, ProviderReply, ProviderSendRequest, ProviderSendResult


class WebhookProvider(MessageProvider):
    def __init__(self, send_url: str = "", replies_url: str = "", timeout_seconds: int = 10) -> None:
        self.send_url = send_url.strip()
        self.replies_url = replies_url.strip()
        self.timeout_seconds = timeout_seconds

    async def send(self, account: ProviderAccount, request: ProviderSendRequest) -> ProviderSendResult:
        if not self.send_url:
            raise ValueError("Provider send URL is not configured.")
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                self.send_url,
                json={
                    "to": request.to,
                    "subject": request.subject,
                    "body": request.body,
                    "thread_id": request.thread_id,
                    "metadata": request.metadata,
                },
            )
            response.raise_for_status()
            raw = response.json()
        return ProviderSendResult(
            message_id=str(raw.get("message_id", raw.get("id", ""))),
            thread_id=str(raw.get("thread_id", raw.get("conversation_id", ""))),
            raw=dict(raw),
        )

    async def fetch_replies(self, account: ProviderAccount, cursor: str = "") -> List[ProviderReply]:
        if not self.replies_url:
            return []
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.get(self.replies_url, params={"cursor": cursor})
            response.raise_for_status()
            payload = response.json()
        items = payload if isinstance(payload, list) else payload.get("items", [])
        replies: list[ProviderReply] = []
        for item in items:
            replies.append(
                ProviderReply(
                    from_address=str(item.get("from", item.get("from_address", ""))),
                    subject=str(item.get("subject", "")),
                    body=str(item.get("body", "")),
                    message_id=str(item.get("message_id", item.get("id", ""))),
                    thread_id=str(item.get("thread_id", item.get("conversation_id", ""))),
                    metadata=dict(item),
                )
            )
        return replies
