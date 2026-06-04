"""Tenant-scoped Gmail provider adapter."""

from __future__ import annotations

from email.mime.text import MIMEText
import asyncio
import base64
from typing import List

try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
except Exception:  # pragma: no cover - optional dependency path
    Request = None
    Credentials = None
    build = None

import leads as legacy_leads

from app.providers.base import MessageProvider, ProviderAccount, ProviderReply, ProviderSendRequest, ProviderSendResult


class GmailProvider(MessageProvider):
    def _credentials(self, account: ProviderAccount) -> Credentials:
        if Credentials is None or Request is None:
            raise RuntimeError("Google API dependencies are not installed.")
        credentials = Credentials(
            token=account.access_token or None,
            refresh_token=account.refresh_token or None,
            token_uri=account.token_uri,
            client_id=account.client_id or None,
            client_secret=account.client_secret or None,
            scopes=account.scopes or None,
        )
        if credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        return credentials

    def _build_service(self, account: ProviderAccount):
        if build is None:
            raise RuntimeError("Google API dependencies are not installed.")
        return build("gmail", "v1", credentials=self._credentials(account), cache_discovery=False)

    async def send(self, account: ProviderAccount, request: ProviderSendRequest) -> ProviderSendResult:
        service = self._build_service(account)
        message = MIMEText(request.body, "plain", "utf-8")
        message["To"] = request.to
        message["Subject"] = request.subject
        payload = {"raw": base64.urlsafe_b64encode(message.as_bytes()).decode()}
        if request.thread_id:
            payload["threadId"] = request.thread_id
        raw = await asyncio.to_thread(
            lambda: service.users().messages().send(userId="me", body=payload).execute()
        )
        return ProviderSendResult(
            message_id=str(raw.get("id", "")),
            thread_id=str(raw.get("threadId", "")),
            raw=dict(raw),
        )

    async def fetch_replies(self, account: ProviderAccount, cursor: str = "") -> List[ProviderReply]:
        if not cursor:
            return []
        service = self._build_service(account)
        thread = await asyncio.to_thread(
            lambda: service.users().threads().get(userId="me", id=cursor, format="full").execute()
        )
        messages = thread.get("messages", [])
        replies: list[ProviderReply] = []
        for message in messages:
            payload = message.get("payload", {})
            replies.append(
                ProviderReply(
                    from_address=legacy_leads.extract_email_address(legacy_leads.extract_gmail_header(payload, "From")),
                    subject=legacy_leads.extract_gmail_header(payload, "Subject"),
                    body=legacy_leads.clean_reply_text(legacy_leads.extract_message_text(payload)),
                    message_id=str(message.get("id", "")),
                    thread_id=str(message.get("threadId", "")),
                    metadata={
                        "internal_date": str(message.get("internalDate", "")),
                        "raw": message,
                    },
                )
            )
        return replies
