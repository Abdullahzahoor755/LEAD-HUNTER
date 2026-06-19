"""Async SQLAlchemy repositories with strict tenant scoping."""

from __future__ import annotations

from dataclasses import fields
from datetime import datetime, timezone
from typing import Any, Generic, Iterable, Optional, Sequence, TypeVar

from sqlalchemy import Select, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import AgentRun, Campaign, Email, Followup, Job, Lead, Payment, Reply, Tenant, User, GmailCredential, VoiceCall
from app.models.sqlalchemy import (
    AgentRunRecord,
    CampaignRecord,
    EmailRecord,
    FollowupRecord,
    JobRecord,
    LeadRecord,
    PaymentRecord,
    ReplyRecord,
    TenantRecord,
    UserRecord,
    GmailCredentialRecord,
    VoiceCallRecord,
)

T = TypeVar("T")
R = TypeVar("R")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AsyncRepository(Generic[T, R]):
    record_type: type[R]
    model_type: type[T]

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list(self, tenant_id: str) -> Sequence[T]:
        result = await self.session.execute(self._tenant_query(tenant_id))
        return [self._to_model(item) for item in result.scalars().all()]

    async def list_all(self) -> Sequence[T]:
        result = await self.session.execute(select(self.record_type))  # type: ignore[arg-type]
        return [self._to_model(item) for item in result.scalars().all()]

    async def get(self, tenant_id: str, item_id: str) -> Optional[T]:
        result = await self.session.execute(self._tenant_query(tenant_id, item_id=item_id))
        record = result.scalar_one_or_none()
        return self._to_model(record) if record else None

    async def save(self, item: T) -> T:
        record = await self._get_record(getattr(item, "tenant_id"), getattr(item, "id"))
        if record is None:
            record = self.record_type()  # type: ignore[call-arg]
        self._copy_to_record(item, record)
        self.session.add(record)
        await self.session.flush()
        return self._to_model(record)

    async def delete(self, tenant_id: str, item_id: str) -> bool:
        record = await self._get_record(tenant_id, item_id)
        if record is None:
            return False
        await self.session.delete(record)
        await self.session.flush()
        return True

    async def _get_record(self, tenant_id: str, item_id: str) -> Optional[R]:
        result = await self.session.execute(self._tenant_query(tenant_id, item_id=item_id))
        return result.scalar_one_or_none()

    def _tenant_query(self, tenant_id: str, item_id: str | None = None) -> Select[tuple[R]]:
        filters: list[Any] = [self.record_type.tenant_id == tenant_id]  # type: ignore[attr-defined]
        if item_id is not None:
            filters.append(self.record_type.id == item_id)  # type: ignore[attr-defined]
        return select(self.record_type).where(and_(*filters))

    def _copy_to_record(self, item: T, record: R) -> None:
        payload = {field.name: getattr(item, field.name) for field in fields(item)}
        payload["updated_at"] = _utc_now()
        if not getattr(record, "id", ""):
            payload.setdefault("created_at", item.created_at)
        for key, value in payload.items():
            target_key = "metadata_json" if key == "metadata" else key
            setattr(record, target_key, value)

    def _to_model(self, record: R) -> T:
        payload = {}
        for field in fields(self.model_type):
            source_key = "metadata_json" if field.name == "metadata" else field.name
            payload[field.name] = getattr(record, source_key)
        return self.model_type(**payload)


class AsyncTenantRepository(AsyncRepository[Tenant, TenantRecord]):
    record_type = TenantRecord
    model_type = Tenant


class AsyncUserRepository(AsyncRepository[User, UserRecord]):
    record_type = UserRecord
    model_type = User

    async def find_by_email(self, tenant_id: str, email: str) -> Optional[User]:
        result = await self.session.execute(
            select(UserRecord).where(UserRecord.tenant_id == tenant_id, UserRecord.email == email.strip().lower())
        )
        record = result.scalar_one_or_none()
        return self._to_model(record) if record else None


class AsyncCampaignRepository(AsyncRepository[Campaign, CampaignRecord]):
    record_type = CampaignRecord
    model_type = Campaign


class AsyncLeadRepository(AsyncRepository[Lead, LeadRecord]):
    record_type = LeadRecord
    model_type = Lead

    async def find_by_company_url(self, tenant_id: str, company_url: str) -> Optional[Lead]:
        normalized = company_url.strip().rstrip("/")
        result = await self.session.execute(
            select(LeadRecord)
            .where(LeadRecord.tenant_id == tenant_id, LeadRecord.company_url == normalized)
            .order_by(LeadRecord.created_at)
            .limit(1)
        )
        record = result.scalar_one_or_none()
        return self._to_model(record) if record else None

    async def find_by_email(self, tenant_id: str, email: str) -> Optional[Lead]:
        result = await self.session.execute(
            select(LeadRecord).where(LeadRecord.tenant_id == tenant_id, LeadRecord.email == email.strip().lower())
        )
        record = result.scalar_one_or_none()
        return self._to_model(record) if record else None

    async def bulk_save(self, leads: Iterable[Lead]) -> Sequence[Lead]:
        saved: list[Lead] = []
        for lead in leads:
            saved.append(await self.save(lead))
        return saved


class AsyncEmailRepository(AsyncRepository[Email, EmailRecord]):
    record_type = EmailRecord
    model_type = Email

    async def list_for_lead(self, tenant_id: str, lead_id: str) -> Sequence[Email]:
        result = await self.session.execute(
            select(EmailRecord).where(EmailRecord.tenant_id == tenant_id, EmailRecord.lead_id == lead_id)
        )
        return [self._to_model(item) for item in result.scalars().all()]


class AsyncReplyRepository(AsyncRepository[Reply, ReplyRecord]):
    record_type = ReplyRecord
    model_type = Reply

    async def list_for_lead(self, tenant_id: str, lead_id: str) -> Sequence[Reply]:
        result = await self.session.execute(
            select(ReplyRecord).where(ReplyRecord.tenant_id == tenant_id, ReplyRecord.lead_id == lead_id)
        )
        return [self._to_model(item) for item in result.scalars().all()]


class AsyncVoiceCallRepository(AsyncRepository[VoiceCall, VoiceCallRecord]):
    record_type = VoiceCallRecord
    model_type = VoiceCall

    async def find_by_provider_call_id(self, provider_call_id: str) -> Optional[VoiceCall]:
        normalized = str(provider_call_id or "").strip()
        if not normalized:
            return None
        result = await self.session.execute(
            select(VoiceCallRecord).where(VoiceCallRecord.provider_call_id == normalized).limit(1)
        )
        record = result.scalar_one_or_none()
        return self._to_model(record) if record else None


class AsyncFollowupRepository(AsyncRepository[Followup, FollowupRecord]):
    record_type = FollowupRecord
    model_type = Followup

    async def list_for_lead(self, tenant_id: str, lead_id: str) -> Sequence[Followup]:
        result = await self.session.execute(
            select(FollowupRecord).where(FollowupRecord.tenant_id == tenant_id, FollowupRecord.lead_id == lead_id)
        )
        return [self._to_model(item) for item in result.scalars().all()]


class AsyncAgentRunRepository(AsyncRepository[AgentRun, AgentRunRecord]):
    record_type = AgentRunRecord
    model_type = AgentRun


class AsyncJobRepository(AsyncRepository[Job, JobRecord]):
    record_type = JobRecord
    model_type = Job

    async def next_queued(self, queue: str) -> Optional[Job]:
        result = await self.session.execute(
            select(JobRecord).where(JobRecord.queue == queue, JobRecord.status == "queued").order_by(JobRecord.created_at)
        )
        record = result.scalar_one_or_none()
        return self._to_model(record) if record else None

    async def next_queued_for_tenant(self, tenant_id: str, queue: str) -> Optional[Job]:
        result = await self.session.execute(
            select(JobRecord)
            .where(JobRecord.tenant_id == tenant_id, JobRecord.queue == queue, JobRecord.status == "queued")
            .order_by(JobRecord.created_at)
            .limit(1)
        )
        record = result.scalar_one_or_none()
        return self._to_model(record) if record else None

    async def claim_next_for_tenant(self, tenant_id: str, queue: str, worker_id: str) -> Optional[Job]:
        result = await self.session.execute(
            select(JobRecord)
            .where(
                JobRecord.tenant_id == tenant_id,
                JobRecord.queue == queue,
                JobRecord.status == "queued",
            )
            .order_by(JobRecord.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        record = result.scalar_one_or_none()
        if record is None:
            return None
        record.status = "running"
        record.attempt_count = int(record.attempt_count or 0) + 1
        record.locked_by = worker_id
        record.locked_at = _utc_now()
        record.started_at = record.locked_at
        record.updated_at = record.locked_at
        await self.session.flush()
        return self._to_model(record)

    async def claim_latest_matching_for_tenant(
        self,
        tenant_id: str,
        queue: str,
        worker_id: str,
        job_type: str,
    ) -> Optional[Job]:
        normalized = str(job_type or "").strip()
        if not normalized:
            return await self.claim_next_for_tenant(tenant_id, queue, worker_id)
        result = await self.session.execute(
            select(JobRecord)
            .where(
                JobRecord.tenant_id == tenant_id,
                JobRecord.queue == queue,
                JobRecord.status == "queued",
                or_(JobRecord.name == normalized, JobRecord.job_type == normalized),
            )
            .order_by(JobRecord.created_at.desc())
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        record = result.scalar_one_or_none()
        if record is None:
            return None
        record.status = "running"
        record.attempt_count = int(record.attempt_count or 0) + 1
        record.locked_by = worker_id
        record.locked_at = _utc_now()
        record.started_at = record.locked_at
        record.updated_at = record.locked_at
        await self.session.flush()
        return self._to_model(record)

    async def get_any(self, item_id: str) -> Optional[Job]:
        result = await self.session.execute(select(JobRecord).where(JobRecord.id == item_id))
        record = result.scalar_one_or_none()
        return self._to_model(record) if record else None


class AsyncPaymentRepository(AsyncRepository[Payment, PaymentRecord]):
    record_type = PaymentRecord
    model_type = Payment

    async def find_by_reference(self, tenant_id: str, payment_reference_id: str) -> Optional[Payment]:
        result = await self.session.execute(
            select(PaymentRecord).where(
                PaymentRecord.tenant_id == tenant_id,
                PaymentRecord.payment_reference_id == payment_reference_id,
            )
        )
        record = result.scalar_one_or_none()
        return self._to_model(record) if record else None


class AsyncGmailCredentialRepository(AsyncRepository[GmailCredential, GmailCredentialRecord]):
    record_type = GmailCredentialRecord
    model_type = GmailCredential

    async def find_by_email(self, tenant_id: str, email_address: str) -> Optional[GmailCredential]:
        result = await self.session.execute(
            select(GmailCredentialRecord).where(
                GmailCredentialRecord.tenant_id == tenant_id,
                GmailCredentialRecord.email_address == email_address.strip().lower()
            )
        )
        record = result.scalar_one_or_none()
        return self._to_model(record) if record else None


def build_async_repositories(session: AsyncSession) -> dict[str, object]:
    return {
        "tenants": AsyncTenantRepository(session),
        "users": AsyncUserRepository(session),
        "campaigns": AsyncCampaignRepository(session),
        "leads": AsyncLeadRepository(session),
        "emails": AsyncEmailRepository(session),
        "replies": AsyncReplyRepository(session),
        "voice_calls": AsyncVoiceCallRepository(session),
        "followups": AsyncFollowupRepository(session),
        "agent_runs": AsyncAgentRunRepository(session),
        "jobs": AsyncJobRepository(session),
        "payments": AsyncPaymentRepository(session),
        "gmail_credentials": AsyncGmailCredentialRepository(session),
    }
