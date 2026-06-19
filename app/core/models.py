"""Tenant-aware domain models used across the SaaS application."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid4().hex


@dataclass(slots=True)
class TenantScopedModel:
    tenant_id: str
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def touch(self) -> None:
        self.updated_at = utc_now()


@dataclass(slots=True)
class Tenant(TenantScopedModel):
    name: str = ""
    slug: str = ""
    status: str = "active"
    is_active: bool = True
    subscription_plan: str = "Free"
    subscription_status: str = "active"
    settings: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TenantContext:
    tenant_id: str
    tenant_slug: str = ""
    user_id: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class User(TenantScopedModel):
    email: str = ""
    full_name: str = ""
    password_hash: str = ""
    role: str = "member"
    status: str = "pending"
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Campaign(TenantScopedModel):
    name: str = ""
    description: str = ""
    status: str = "draft"
    channel: str = "email"
    owner_user_id: str = ""
    target_query: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Lead(TenantScopedModel):
    campaign_id: str = ""
    job_id: str = ""
    company_url: str = ""
    verified_email: str = ""
    service_reason: str = ""
    outreach_status: str = "pending"
    followup_count: int = 0
    reply_status: str = "no_reply"
    last_reply_at: Optional[datetime] = None
    company_name: str = ""
    company: str = ""
    website: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    country: str = ""
    city: str = ""
    industry: str = ""
    raw_html: str = ""
    cleaned_text: str = ""
    ai_response: Dict[str, Any] = field(default_factory=dict)
    company_summary: str = ""
    needs_it_services: bool = False
    lead_score: int = 0
    score: int = 0
    reason: str = ""
    buying_intent_score: int = 0
    service_demand_score: int = 0
    urgency_score: int = 0
    intent_summary: str = ""
    signals: List[str] = field(default_factory=list)
    lifecycle_state: str = "discovered"
    status: str = "pending"
    rejection_reason: str = ""
    source_query: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Email(TenantScopedModel):
    campaign_id: str = ""
    lead_id: str = ""
    user_id: str = ""
    subject: str = ""
    body: str = ""
    provider: str = "gmail"
    provider_message_id: str = ""
    provider_thread_id: str = ""
    direction: str = "outbound"
    status: str = "draft"
    sent_at: Optional[datetime] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


OutreachMessage = Email


@dataclass(slots=True)
class Reply(TenantScopedModel):
    campaign_id: str = ""
    lead_id: str = ""
    email_id: str = ""
    provider_message_id: str = ""
    provider_thread_id: str = ""
    from_email: str = ""
    subject: str = ""
    body: str = ""
    classification: str = ""
    sentiment: str = ""
    lead_temperature: str = ""
    received_at: Optional[datetime] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class VoiceCall(TenantScopedModel):
    lead_id: str = ""
    user_id: str = ""
    provider: str = "vapi"
    provider_call_id: str = ""
    phone_number: str = ""
    direction: str = "outbound"
    status: str = "pending"
    outcome: str = ""
    duration_seconds: int = 0
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    transcript: str = ""
    summary: str = ""
    recording_url: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Followup(TenantScopedModel):
    campaign_id: str = ""
    lead_id: str = ""
    email_id: str = ""
    sequence_step: int = 1
    status: str = "scheduled"
    scheduled_for: Optional[datetime] = None
    sent_at: Optional[datetime] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AgentRun(TenantScopedModel):
    agent_name: str = ""
    job_id: str = ""
    status: str = "queued"
    input_payload: Dict[str, Any] = field(default_factory=dict)
    output_payload: Dict[str, Any] = field(default_factory=dict)
    error: str = ""


@dataclass(slots=True)
class Job(TenantScopedModel):
    name: str = ""
    job_type: str = "discovery"
    queue: str = "default"
    status: str = "queued"
    payload: Dict[str, Any] = field(default_factory=dict)
    result: Dict[str, Any] = field(default_factory=dict)
    result_summary: Dict[str, Any] = field(default_factory=dict)
    error: str = ""
    error_log: Dict[str, Any] = field(default_factory=dict)
    attempt_count: int = 0
    retry_count: int = 0
    max_attempts: int = 3
    max_retries: int = 3
    locked_by: str = ""
    scheduled_for: Optional[datetime] = None
    locked_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


@dataclass(slots=True)
class Payment(TenantScopedModel):
    user_id: str = ""
    user_email: str = ""
    full_name: str = ""
    phone_number: str = ""
    whatsapp_number: str = ""
    plan: str = ""
    amount: int = 0
    currency: str = "PKR"
    status: str = "pending"
    payment_method: str = ""
    payment_reference_id: str = ""
    transaction_reference: str = ""
    proof_url: str = ""
    user_note: str = ""
    admin_note: str = ""
    reviewed_by: str = ""
    reviewed_at: Optional[datetime] = None
    approved_at: Optional[datetime] = None
    rejected_at: Optional[datetime] = None


@dataclass(slots=True)
class GmailCredential(TenantScopedModel):
    email_address: str = ""
    credentials_json: str = ""
