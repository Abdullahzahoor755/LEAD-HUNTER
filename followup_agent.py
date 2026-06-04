"""
Follow-up automation AI agent.
"""

import asyncio
from datetime import timedelta
from typing import Dict, List

from app.core.models import TenantContext
from leads import (
    LOGGER,
    generate_followup_email,
    list_followup_candidates,
    load_environment,
    now_utc,
    parse_datetime,
    append_unsubscribe_footer,
    is_opted_out_domain,
    save_followup_result,
    send_email_gmail,
    to_iso8601,
)


def _needs_followup(row: Dict[str, str]) -> bool:
    if row.get("EmailStatus", "").strip().lower() != "sent":
        return False
    if row.get("ReplyStatus", "").strip().lower() == "received":
        return False
    followup_count = int(row.get("FollowupCount", "0") or "0")
    if followup_count >= 3:
        return False
    sent_at = parse_datetime(row.get("LastContactedAt", "") or row.get("EmailSentAt", ""))
    if not sent_at:
        return False
    next_due = parse_datetime(row.get("NextFollowupDue", ""))
    now = now_utc()
    if next_due:
        return now >= next_due
    return now >= sent_at + timedelta(days=2)


def _build_followup_updates(row: Dict[str, str], subject: str, body: str, thread_id: str) -> Dict[str, str]:
    current_count = int(row.get("FollowupCount", "0") or "0")
    new_count = current_count + 1
    next_due = ""
    if new_count < 3:
        next_due = to_iso8601(now_utc() + timedelta(days=2))
    return {
        "FollowupCount": str(new_count),
        "LastFollowupDate": to_iso8601(),
        "NextFollowupDue": next_due,
        "LastContactedAt": to_iso8601(),
        "LastEmailBody": body,
        "EmailSubject": subject,
        "GmailThreadId": thread_id or row.get("GmailThreadId", ""),
    }


def run_followups(tenant: TenantContext) -> List[Dict[str, str]]:
    """
    Send timed follow-up emails for leads that were contacted, have not replied,
    and are due for the next sequence touchpoint.
    """
    load_environment()
    print("Running follow-up automation...")
    sent_followups: List[Dict[str, str]] = []

    async def _run() -> List[Dict[str, str]]:
        for item in await list_followup_candidates(tenant):
            lead = item["lead"]
            last_email = item["last_email"]
            email = lead.email.strip()
            if lead.status.lower() == "unsubscribed" or is_opted_out_domain(email):
                continue
            row = {
                "Company": lead.company,
                "Reason": lead.reason,
                "EmailSubject": getattr(last_email, "subject", ""),
                "LastEmailBody": getattr(last_email, "body", ""),
                "FollowupCount": str(int((lead.metadata or {}).get("FollowupCount", 0) or 0)),
                "GmailThreadId": str((lead.metadata or {}).get("GmailThreadId", getattr(last_email, "provider_thread_id", ""))),
            }
            company = lead.company.strip() or "Unknown Company"
            thread_id = row["GmailThreadId"].strip()
            followup_number = int(row.get("FollowupCount", "0") or "0") + 1
            try:
                subject, body = generate_followup_email(row, followup_number)
                body = append_unsubscribe_footer(body)
                result = send_email_gmail(email, subject, body, thread_id=thread_id)
                updated_thread_id = str(result.get("threadId", "")) or thread_id
                await save_followup_result(
                    lead,
                    last_email_id=getattr(last_email, "id", ""),
                    subject=subject,
                    body=body,
                    thread_id=updated_thread_id,
                    followup_number=followup_number,
                )
                sent_followups.append({"email": email, "company": company, "followup_number": str(followup_number)})
                print("Follow-up email sent.")
            except Exception as error:
                LOGGER.error("Follow-up failed for %s (%s): %s", company, email, error)
        LOGGER.info("Follow-up automation completed. Sent %s follow-up(s).", len(sent_followups))
        return sent_followups

    return asyncio.run(_run())


if __name__ == "__main__":
    run_followups()
