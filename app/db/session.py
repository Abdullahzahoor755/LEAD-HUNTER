"""Database session facades used by services, workers, and async APIs."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
import re
from typing import Any, Sequence

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.configs.settings import settings
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
from app.core.models import TenantContext
from app.core.tenant import assert_same_tenant, get_current_tenant, resolve_tenant_context
from app.db.memory import (
    InMemoryAgentRunRepository,
    InMemoryCampaignRepository,
    InMemoryEmailRepository,
    InMemoryFollowupRepository,
    InMemoryJobRepository,
    InMemoryLeadRepository,
    InMemoryPaymentRepository,
    InMemoryReplyRepository,
    InMemoryTenantRepository,
    InMemoryUserRepository,
    InMemoryGmailCredentialRepository,
)
from app.repositories.sqlalchemy import build_async_repositories


@dataclass(slots=True)
class DatabaseSession:
    tenants: TenantRepository
    users: UserRepository
    campaigns: CampaignRepository
    leads: LeadRepository
    emails: EmailRepository
    replies: ReplyRepository
    followups: FollowupRepository
    agent_runs: AgentRunRepository
    jobs: JobRepository
    payments: PaymentRepository
    gmail_credentials: GmailCredentialRepository

    def for_tenant(self, tenant: TenantContext | str | None = None) -> "TenantScopedSession":
        resolved = tenant
        if resolved is None:
            resolved = get_current_tenant()
        elif isinstance(resolved, str):
            resolved = resolve_tenant_context(resolved)
        return TenantScopedSession(base=self, tenant=resolved)


@dataclass(slots=True)
class TenantScopedSession:
    base: DatabaseSession
    tenant: TenantContext

    def assert_item_tenant(self, item: Any) -> Any:
        item_tenant_id = getattr(item, "tenant_id", "")
        if item_tenant_id:
            assert_same_tenant(self.tenant.tenant_id, item_tenant_id)
        return item

    def list(self, repository_name: str) -> Sequence[Any]:
        repository = getattr(self.base, repository_name)
        return repository.list(self.tenant.tenant_id)

    def get(self, repository_name: str, item_id: str) -> Any:
        repository = getattr(self.base, repository_name)
        item = repository.get(self.tenant.tenant_id, item_id)
        if item is not None:
            self.assert_item_tenant(item)
        return item

    def save(self, repository_name: str, item: Any) -> Any:
        self.assert_item_tenant(item)
        repository = getattr(self.base, repository_name)
        return repository.save(item)

    def delete(self, repository_name: str, item_id: str) -> bool:
        repository = getattr(self.base, repository_name)
        return repository.delete(self.tenant.tenant_id, item_id)


@dataclass(slots=True)
class AsyncDatabaseSession:
    session: AsyncSession
    tenants: Any
    users: Any
    campaigns: Any
    leads: Any
    emails: Any
    replies: Any
    followups: Any
    agent_runs: Any
    jobs: Any
    payments: Any
    gmail_credentials: Any

    def for_tenant(self, tenant: TenantContext | str | None = None) -> "AsyncTenantScopedSession":
        resolved = tenant
        if resolved is None:
            resolved = get_current_tenant()
        elif isinstance(resolved, str):
            resolved = resolve_tenant_context(resolved)
        return AsyncTenantScopedSession(base=self, tenant=resolved)


@dataclass(slots=True)
class AsyncTenantScopedSession:
    base: AsyncDatabaseSession
    tenant: TenantContext

    def assert_item_tenant(self, item: Any) -> Any:
        item_tenant_id = getattr(item, "tenant_id", "")
        if item_tenant_id:
            assert_same_tenant(self.tenant.tenant_id, item_tenant_id)
        return item

    async def list(self, repository_name: str) -> Sequence[Any]:
        repository = getattr(self.base, repository_name)
        return await repository.list(self.tenant.tenant_id)

    async def get(self, repository_name: str, item_id: str) -> Any:
        repository = getattr(self.base, repository_name)
        item = await repository.get(self.tenant.tenant_id, item_id)
        if item is not None:
            self.assert_item_tenant(item)
        return item

    async def save(self, repository_name: str, item: Any) -> Any:
        self.assert_item_tenant(item)
        repository = getattr(self.base, repository_name)
        return await repository.save(item)

    async def delete(self, repository_name: str, item_id: str) -> bool:
        repository = getattr(self.base, repository_name)
        return await repository.delete(self.tenant.tenant_id, item_id)


def build_memory_session() -> DatabaseSession:
    return DatabaseSession(
        tenants=InMemoryTenantRepository(),
        users=InMemoryUserRepository(),
        campaigns=InMemoryCampaignRepository(),
        leads=InMemoryLeadRepository(),
        emails=InMemoryEmailRepository(),
        replies=InMemoryReplyRepository(),
        followups=InMemoryFollowupRepository(),
        agent_runs=InMemoryAgentRunRepository(),
        jobs=InMemoryJobRepository(),
        payments=InMemoryPaymentRepository(),
        gmail_credentials=InMemoryGmailCredentialRepository(),
    )


_ASYNC_ENGINE = None
_ASYNC_FACTORY: async_sessionmaker[AsyncSession] | None = None


def normalize_async_database_url(database_url: str) -> str:
    normalized = str(database_url or "").strip()
    if not normalized:
        raise ValueError("DATABASE_URL is required when DATABASE_BACKEND=postgres.")
    if normalized.startswith("postgres://"):
        return "postgresql+asyncpg://" + normalized[len("postgres://") :]
    if normalized.startswith("postgresql://") and "+asyncpg" not in normalized:
        return re.sub(r"^postgresql://", "postgresql+asyncpg://", normalized, count=1)
    return normalized


def get_async_engine():
    global _ASYNC_ENGINE
    if _ASYNC_ENGINE is None:
        _ASYNC_ENGINE = create_async_engine(
            normalize_async_database_url(settings.database_url),
            echo=settings.database_echo,
            future=True,
            pool_pre_ping=True,
        )
    return _ASYNC_ENGINE


def get_async_session_factory() -> async_sessionmaker[AsyncSession]:
    global _ASYNC_FACTORY
    if _ASYNC_FACTORY is None:
        _ASYNC_FACTORY = async_sessionmaker(get_async_engine(), expire_on_commit=False, class_=AsyncSession)
    return _ASYNC_FACTORY


async def reset_async_session_factory() -> None:
    global _ASYNC_ENGINE, _ASYNC_FACTORY
    if _ASYNC_ENGINE is not None:
        await _ASYNC_ENGINE.dispose()
    _ASYNC_ENGINE = None
    _ASYNC_FACTORY = None


def build_async_session(session: AsyncSession) -> AsyncDatabaseSession:
    repositories = build_async_repositories(session)
    return AsyncDatabaseSession(session=session, **repositories)


@asynccontextmanager
async def get_async_db_session() -> Any:
    session_factory = get_async_session_factory()
    async with session_factory() as session:
        db = build_async_session(session)
        try:
            yield db
            await session.commit()
        except Exception:
            await session.rollback()
            raise
