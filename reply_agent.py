"""
Reply detection AI agent.
"""

import asyncio
from typing import Any, Dict, List

from app.core.models import TenantContext
from leads import (
    LOGGER,
    analyze_reply_with_claude,
    clean_reply_text,
    extract_email_address,
    extract_gmail_header,
    get_gmail_profile_email,
    extract_message_text,
    get_thread_messages,
    list_reply_candidates,
    
    load_environment,
    parse_gmail_internal_date,
    save_reply_result,
)


def _is_inbound_reply(sender_email: str, lead_email: str, my_email: str) -> bool:
    normalized_sender = sender_email.strip().lower()
    normalized_lead = lead_email.strip().lower()
    normalized_me = my_email.strip().lower()
    if not normalized_sender:
        return False
    if normalized_me and normalized_sender == normalized_me:
        return False
    if normalized_lead and normalized_sender == normalized_lead:
        return True
    return True


def _build_reply_update(row: Dict[str, str], message: Dict[str, Any]) -> Dict[str, str]:
    payload = message.get("payload", {})
    sender = extract_gmail_header(payload, "From")
    sender_email = extract_email_address(sender)
    subject = extract_gmail_header(payload, "Subject")
    reply_text = clean_reply_text(extract_message_text(payload))
    analysis = analyze_reply_with_claude(
        company=row.get("Company", ""),
        sender_email=sender_email,
        subject=subject,
        reply_text=reply_text,
    )
    classification = analysis.get("classification", "")
    return {
        "ReplyStatus": "Received",
        "ReplyClassification": str(classification),
        "Sentiment": str(analysis.get("sentiment", "")),
        "LeadTemperature": str(analysis.get("lead_temperature", "")),
        "LastReply": str(analysis.get("reason", "")),
        "LastReplyAt": parse_gmail_internal_date(message),
        "LastReplyFrom": sender_email,
        "LastReplySnippet": reply_text[:500],
        "MeetingRequested": "Yes" if classification == "Interested" else row.get("MeetingRequested", "No"),
        "NextFollowupDue": "",
        "ReplyConfidenceScore": str(analysis.get("confidence_score", "")),
        "NextActionSuggestion": str(analysis.get("next_action_suggestion", "")),
    }


def _find_latest_inbound_message(row: Dict[str, str]) -> Dict[str, Any]:
    thread_id = row.get("GmailThreadId", "").strip()
    if not thread_id:
        return {}
    my_email = get_gmail_profile_email()
    messages = get_thread_messages(thread_id)
    inbound_messages: List[Dict[str, Any]] = []
    for message in messages:
        payload = message.get("payload", {})
        sender_email = extract_email_address(extract_gmail_header(payload, "From"))
        if _is_inbound_reply(sender_email, row.get("Email", ""), my_email):
            inbound_messages.append(message)
    if not inbound_messages:
        return {}
    inbound_messages.sort(key=lambda item: int(item.get("internalDate", "0") or "0"))
    return inbound_messages[-1]


def check_email_replies(tenant: TenantContext) -> List[Dict[str, str]]:
    """
    Check Gmail threads for lead replies, classify them with Claude, and sync
    the resulting status back to CSV and Google Sheets.
    """
    load_environment()
    print("Checking inbox replies...")
    processed_replies: List[Dict[str, str]] = []

    async def _run() -> List[Dict[str, str]]:
        for item in await list_reply_candidates(tenant):
            lead = item["lead"]
            row = {
                "Company": lead.company,
                "Email": lead.email,
                "Website": lead.website,
                "GmailThreadId": item["thread_id"],
                "LastReplyAt": str((lead.metadata or {}).get("LastReplyAt", "")),
                "MeetingRequested": str((lead.metadata or {}).get("MeetingRequested", "No")),
            }
            latest_reply = _find_latest_inbound_message(row)
            if not latest_reply:
                continue
            latest_reply_at = parse_gmail_internal_date(latest_reply)
            if latest_reply_at and latest_reply_at == row.get("LastReplyAt", "").strip():
                continue
            payload = latest_reply.get("payload", {})
            sender_email = extract_email_address(extract_gmail_header(payload, "From"))
            print(f"Reply detected from: {sender_email}")
            updates = _build_reply_update(row, latest_reply)
            print(f"Classification: {updates.get('ReplyClassification', '')}")
            await save_reply_result(lead, getattr(item["last_email"], "id", ""), latest_reply, updates)
            processed_replies.append(
                {
                    "email": lead.email,
                    "classification": updates.get("ReplyClassification", ""),
                    "sentiment": updates.get("Sentiment", ""),
                    "confidence_score": updates.get("ReplyConfidenceScore", ""),
                    "next_action_suggestion": updates.get("NextActionSuggestion", ""),
                }
            )
        LOGGER.info("Reply detection completed. Processed %s reply/replies.", len(processed_replies))
        return processed_replies

    return asyncio.run(_run())


if __name__ == "__main__":
    check_email_replies()
