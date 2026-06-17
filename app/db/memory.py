"""In-memory repositories for local development and testing."""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Sequence, Type, TypeVar

from app.core.interfaces import (
    AgentRunRepository,
    CampaignRepository,
    EmailRepository,
    FollowupRepository,
    JobRepository,
    LeadRepository,
    PaymentRepository,
    ReplyRepository,
    TenantRepository,
    UserRepository,
    GmailCredentialRepository,
)
from app.core.models import AgentRun, Campaign, Email, Followup, Job, Lead, Payment, Reply, Tenant, User, GmailCredential, utc_now
from app.core.tenant import assert_same_tenant
from app.db.base import deserialize_model, serialize_model

T = TypeVar("T")


class InMemoryTenantScopedRepository:
    def __init__(self, model_type: Type[T]) -> None:
        self.model_type = model_type
        self._items: Dict[str, Dict[str, Dict[str, object]]] = defaultdict(dict)

    def list(self, tenant_id: str) -> Sequence[T]:
        return [deserialize_model(self.model_type, item) for item in self._items[tenant_id].values()]

    def list_all(self) -> Sequence[T]:
        return [
            deserialize_model(self.model_type, item)
            for tenant_items in self._items.values()
            for item in tenant_items.values()
        ]

    def get(self, tenant_id: str, item_id: str) -> Optional[T]:
        payload = self._items[tenant_id].get(item_id)
        if not payload:
            return None
        return deserialize_model(self.model_type, payload)

    def save(self, item: T) -> T:
        tenant_id = getattr(item, "tenant_id")
        item.touch()
        self._items[tenant_id][getattr(item, "id")] = serialize_model(item)
        return item

    def delete(self, tenant_id: str, item_id: str) -> bool:
        return self._items[tenant_id].pop(item_id, None) is not None


class InMemoryTenantRepository(InMemoryTenantScopedRepository, TenantRepository):
    def __init__(self) -> None:
        super().__init__(Tenant)


class InMemoryUserRepository(InMemoryTenantScopedRepository, UserRepository):
    def __init__(self) -> None:
        super().__init__(User)

    def find_by_email(self, tenant_id: str, email: str) -> Optional[User]:
        normalized = email.strip().lower()
        for item in self.list(tenant_id):
            if item.email.strip().lower() == normalized:
                return item
        return None


class InMemoryCampaignRepository(InMemoryTenantScopedRepository, CampaignRepository):
    def __init__(self) -> None:
        super().__init__(Campaign)


class InMemoryLeadRepository(InMemoryTenantScopedRepository, LeadRepository):
    def __init__(self) -> None:
        super().__init__(Lead)

    def find_by_company_url(self, tenant_id: str, company_url: str) -> Optional[Lead]:
        normalized = company_url.strip().lower().rstrip("/")
        for item in self.list(tenant_id):
            if item.company_url.strip().lower().rstrip("/") == normalized:
                return item
        return None

    def find_by_email(self, tenant_id: str, email: str) -> Optional[Lead]:
        normalized = email.strip().lower()
        for item in self.list(tenant_id):
            if item.email.strip().lower() == normalized:
                return item
        return None

    def bulk_save(self, leads: Iterable[Lead]) -> Sequence[Lead]:
        saved: List[Lead] = []
        for lead in leads:
            saved.append(self.save(lead))
        return saved


class InMemoryEmailRepository(InMemoryTenantScopedRepository, EmailRepository):
    def __init__(self) -> None:
        super().__init__(Email)

    def list_for_lead(self, tenant_id: str, lead_id: str) -> Sequence[Email]:
        return [item for item in self.list(tenant_id) if item.lead_id == lead_id]


class InMemoryReplyRepository(InMemoryTenantScopedRepository, ReplyRepository):
    def __init__(self) -> None:
        super().__init__(Reply)

    def list_for_lead(self, tenant_id: str, lead_id: str) -> Sequence[Reply]:
        return [item for item in self.list(tenant_id) if item.lead_id == lead_id]


class InMemoryFollowupRepository(InMemoryTenantScopedRepository, FollowupRepository):
    def __init__(self) -> None:
        super().__init__(Followup)

    def list_for_lead(self, tenant_id: str, lead_id: str) -> Sequence[Followup]:
        return [item for item in self.list(tenant_id) if item.lead_id == lead_id]


class InMemoryAgentRunRepository(InMemoryTenantScopedRepository, AgentRunRepository):
    def __init__(self) -> None:
        super().__init__(AgentRun)


class InMemoryJobRepository(InMemoryTenantScopedRepository, JobRepository):
    def __init__(self) -> None:
        super().__init__(Job)

    def next_queued(self, queue: str) -> Optional[Job]:
        for tenant_items in self._items.values():
            for payload in tenant_items.values():
                if payload.get("queue") == queue and payload.get("status") == "queued":
                    job = deserialize_model(Job, payload)
                    assert_same_tenant(job.tenant_id, str(payload.get("tenant_id")))
                    return job
        return None

    def next_queued_for_tenant(self, tenant_id: str, queue: str) -> Optional[Job]:
        for payload in self._items[tenant_id].values():
            if payload.get("queue") == queue and payload.get("status") == "queued":
                job = deserialize_model(Job, payload)
                assert_same_tenant(job.tenant_id, tenant_id)
                return job
        return None

    def claim_next_for_tenant(self, tenant_id: str, queue: str, worker_id: str) -> Optional[Job]:
        for item_id, payload in self._items[tenant_id].items():
            if payload.get("queue") != queue or payload.get("status") != "queued":
                continue
            job = deserialize_model(Job, payload)
            assert_same_tenant(job.tenant_id, tenant_id)
            job.status = "running"
            job.attempt_count = int(job.attempt_count or 0) + 1
            job.locked_by = worker_id
            job.started_at = job.started_at or utc_now()
            self.save(job)
            self._items[tenant_id][item_id]["locked_at"] = serialize_model(job).get("updated_at")
            return job
        return None

    def claim_latest_matching_for_tenant(self, tenant_id: str, queue: str, worker_id: str, job_type: str) -> Optional[Job]:
        normalized = str(job_type or "").strip()
        if not normalized:
            return self.claim_next_for_tenant(tenant_id, queue, worker_id)
        candidates = []
        for item_id, payload in self._items[tenant_id].items():
            if payload.get("queue") != queue or payload.get("status") != "queued":
                continue
            if payload.get("name") != normalized and payload.get("job_type") != normalized:
                continue
            candidates.append((payload.get("created_at", ""), item_id, payload))
        if not candidates:
            return None
        _, item_id, payload = sorted(candidates, key=lambda item: item[0], reverse=True)[0]
        job = deserialize_model(Job, payload)
        assert_same_tenant(job.tenant_id, tenant_id)
        job.status = "running"
        job.attempt_count = int(job.attempt_count or 0) + 1
        job.locked_by = worker_id
        job.started_at = job.started_at or utc_now()
        self.save(job)
        self._items[tenant_id][item_id]["locked_at"] = serialize_model(job).get("updated_at")
        return job

    def get_any(self, item_id: str) -> Optional[Job]:
        for tenant_items in self._items.values():
            payload = tenant_items.get(item_id)
            if payload:
                job = deserialize_model(Job, payload)
                assert_same_tenant(job.tenant_id, str(payload.get("tenant_id")))
                return job
        return None


class InMemoryPaymentRepository(InMemoryTenantScopedRepository, PaymentRepository):
    def __init__(self) -> None:
        super().__init__(Payment)

    def find_by_reference(self, tenant_id: str, payment_reference_id: str) -> Optional[Payment]:
        normalized = payment_reference_id.strip()
        for item in self.list(tenant_id):
            if item.payment_reference_id.strip() == normalized:
                return item
        return None


class InMemoryGmailCredentialRepository(InMemoryTenantScopedRepository, GmailCredentialRepository):
    def __init__(self) -> None:
        super().__init__(GmailCredential)

    def find_by_email(self, tenant_id: str, email_address: str) -> Optional[GmailCredential]:
        normalized = email_address.strip().lower()
        for item in self.list(tenant_id):
            if item.email_address.strip().lower() == normalized:
                return item
        return None
