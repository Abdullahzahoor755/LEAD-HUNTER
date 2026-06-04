"""Small wrapper around the existing reply-classification AI flow."""

from __future__ import annotations

from typing import Any, Dict

import leads as legacy_leads


class ReplyAiService:
    async def classify(self, company: str, sender_email: str, subject: str, reply_text: str) -> Dict[str, Any]:
        return legacy_leads.analyze_reply_with_claude(
            company=company,
            sender_email=sender_email,
            subject=subject,
            reply_text=reply_text,
        )
