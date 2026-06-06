"""Safe outreach failure codes shared by API, workers, UI helpers, and scripts."""

from __future__ import annotations


UNKNOWN_OUTREACH_FAILURE = "unknown_outreach_failure"

SAFE_OUTREACH_FAILURE_REASONS = {
    "no_verified_email",
    "missing_gmail_credentials",
    "gmail_send_failed",
    "oauth_token_error",
    "plan_locked",
    "provider_generation_failed",
    UNKNOWN_OUTREACH_FAILURE,
}

OUTREACH_ERROR_MESSAGES = {
    UNKNOWN_OUTREACH_FAILURE: "This failed before detailed diagnostics were enabled. Re-run outreach to get the exact reason.",
}


def safe_outreach_error(value: str) -> str:
    candidate = str(value or "").strip().lower()
    return candidate if candidate in SAFE_OUTREACH_FAILURE_REASONS else ""


def normalized_outreach_error(value: str, *, status: str = "", outreach_status: str = "") -> str:
    safe = safe_outreach_error(value)
    if safe:
        return safe
    if str(status or "").strip().lower() == "failed" or str(outreach_status or "").strip().lower() == "failed":
        return UNKNOWN_OUTREACH_FAILURE
    return ""


def outreach_error_message(value: str) -> str:
    safe = safe_outreach_error(value) or str(value or "").strip().lower()
    return OUTREACH_ERROR_MESSAGES.get(safe, safe)
