"""Provider registry used by services and agents."""

from __future__ import annotations

import os

from app.providers.base import MessageProvider
from app.providers.gmail import GmailProvider
from app.providers.webhook import WebhookProvider


def build_provider_registry() -> dict[str, MessageProvider]:
    return {
        "gmail": GmailProvider(),
        "outlook": WebhookProvider(
            send_url=os.getenv("OUTLOOK_PROVIDER_SEND_URL", ""),
            replies_url=os.getenv("OUTLOOK_PROVIDER_REPLIES_URL", ""),
        ),
        "twilio": WebhookProvider(
            send_url=os.getenv("TWILIO_PROVIDER_SEND_URL", ""),
            replies_url=os.getenv("TWILIO_PROVIDER_REPLIES_URL", ""),
        ),
        "whatsapp": WebhookProvider(
            send_url=os.getenv("WHATSAPP_PROVIDER_SEND_URL", ""),
            replies_url=os.getenv("WHATSAPP_PROVIDER_REPLIES_URL", ""),
        ),
    }
