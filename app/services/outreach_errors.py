"""Safe outreach failure codes shared by API, workers, UI helpers, and scripts."""

from __future__ import annotations


UNKNOWN_OUTREACH_FAILURE = "unknown_outreach_failure"

SAFE_OUTREACH_FAILURE_REASONS = {
    "no_verified_email",
    "missing_gmail_credentials",
    "gmail_send_failed",
    "gmail_api_disabled",
    "gmail_not_connected",
    "gmail_token_expired",
    "gmail_refresh_failed",
    "gmail_missing_refresh_token",
    "gmail_insufficient_scopes",
    "gmail_invalid_recipient",
    "gmail_quota_exceeded",
    "gmail_sender_not_verified",
    "gmail_network_error",
    "gmail_unknown_send_error",
    "oauth_token_error",
    "plan_locked",
    "provider_generation_failed",
    "demo_mode_enabled",
    UNKNOWN_OUTREACH_FAILURE,
}

OUTREACH_ERROR_MESSAGES = {
    UNKNOWN_OUTREACH_FAILURE: "This failed before detailed diagnostics were enabled. Re-run outreach to get the exact reason.",
    "gmail_api_disabled": "Gmail API is disabled in Google Cloud. Enable Gmail API for this OAuth project, then reconnect Gmail.",
    "gmail_not_connected": "Gmail is not connected for this workspace.",
    "gmail_missing_refresh_token": "Gmail reconnect is required because no refresh token is stored.",
    "gmail_insufficient_scopes": "Gmail permissions are incomplete. Reconnect Gmail and approve send/read access.",
    "gmail_invalid_recipient": "The recipient email address was rejected by Gmail.",
    "gmail_quota_exceeded": "Gmail quota or rate limit was exceeded. Try again later.",
    "gmail_network_error": "Gmail request failed due to a network error.",
    "gmail_unknown_send_error": "Gmail send failed with an unknown safe error.",
    "demo_mode_enabled": "Demo mode is enabled. No real email was sent.",
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
