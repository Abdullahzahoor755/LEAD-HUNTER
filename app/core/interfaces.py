"""Repository and service interfaces for the tenant-aware application."""

from __future__ import annotations

from typing import Iterable, Optional, Protocol, Sequence, TypeVar

from app.core.models import AgentRun, Campaign, Email, Followup, Job, Lead, Payment, Reply, Tenant, User, GmailCredential

T = TypeVar("T")


class TenantScopedRepository(Protocol[T]):
    def list(self, tenant_id: str) -> Sequence[T]:
        ...

    def list_all(self) -> Sequence[T]:
        ...

    def get(self, tenant_id: str, item_id: str) -> Optional[T]:
        ...

    def save(self, item: T) -> T:
        ...

    def delete(self, tenant_id: str, item_id: str) -> bool:
        ...


class TenantRepository(TenantScopedRepository[Tenant], Protocol):
    pass


class UserRepository(TenantScopedRepository[User], Protocol):
    def find_by_email(self, tenant_id: str, email: str) -> Optional[User]:
        ...


class CampaignRepository(TenantScopedRepository[Campaign], Protocol):
    pass


class LeadRepository(TenantScopedRepository[Lead], Protocol):
    def find_by_company_url(self, tenant_id: str, company_url: str) -> Optional[Lead]:
        ...

    def find_by_email(self, tenant_id: str, email: str) -> Optional[Lead]:
        ...

    def bulk_save(self, leads: Iterable[Lead]) -> Sequence[Lead]:
        ...


class EmailRepository(TenantScopedRepository[Email], Protocol):
    def list_for_lead(self, tenant_id: str, lead_id: str) -> Sequence[Email]:
        ...


class ReplyRepository(TenantScopedRepository[Reply], Protocol):
    def list_for_lead(self, tenant_id: str, lead_id: str) -> Sequence[Reply]:
        ...


class FollowupRepository(TenantScopedRepository[Followup], Protocol):
    def list_for_lead(self, tenant_id: str, lead_id: str) -> Sequence[Followup]:
        ...


class AgentRunRepository(TenantScopedRepository[AgentRun], Protocol):
    pass


class JobRepository(TenantScopedRepository[Job], Protocol):
    def next_queued(self, queue: str) -> Optional[Job]:
        ...

    def next_queued_for_tenant(self, tenant_id: str, queue: str) -> Optional[Job]:
        ...

    def claim_next_for_tenant(self, tenant_id: str, queue: str, worker_id: str) -> Optional[Job]:
        ...

    def get_any(self, item_id: str) -> Optional[Job]:
        ...


class PaymentRepository(TenantScopedRepository[Payment], Protocol):
    def find_by_reference(self, tenant_id: str, payment_reference_id: str) -> Optional[Payment]:
        ...


class GmailCredentialRepository(TenantScopedRepository[GmailCredential], Protocol):
    def find_by_email(self, tenant_id: str, email_address: str) -> Optional[GmailCredential]:
        ...
