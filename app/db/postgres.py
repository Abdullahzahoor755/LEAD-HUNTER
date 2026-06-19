"""Async SQLAlchemy backend helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Sequence, TypeVar

from sqlalchemy import text

from app.models.sqlalchemy import Base
from app.db.session import DatabaseSession, get_async_engine, get_async_session_factory
from app.repositories.sqlalchemy import build_async_repositories

T = TypeVar("T")

SCHEMA_PATH = Path(__file__).resolve().parent / "sql" / "schema.sql"
SCORE_BREAKDOWN_SQL_PATTERNS = (
    "%email=%/%",
    "%phone=%/%",
    "%relevance=%",
    "%quality=%",
    "%minimum_fallback=%",
    "%missing email -> marked no_email%",
)
SCORE_BREAKDOWN_MARKERS = (
    "email=",
    "phone=",
    "relevance=",
    "quality=",
    "minimum_fallback=",
    "missing email -> marked no_email",
)


def load_postgres_schema() -> str:
    return SCHEMA_PATH.read_text(encoding="utf-8")


def looks_like_score_breakdown_reason(value: str) -> bool:
    lowered = str(value or "").lower()
    return any(marker in lowered for marker in SCORE_BREAKDOWN_MARKERS)


def service_reason_backfill_sql() -> str:
    rejected_reason_predicate = " OR ".join(
        f"LOWER(COALESCE(reason, '')) LIKE '{pattern}'" for pattern in SCORE_BREAKDOWN_SQL_PATTERNS
    )
    return (
        "UPDATE leads "
        "SET service_reason = NULLIF(reason, '') "
        "WHERE service_reason = '' "
        "AND NULLIF(reason, '') IS NOT NULL "
        f"AND NOT ({rejected_reason_predicate})"
    )


async def apply_additive_lead_migration(connection) -> None:
    statements = [
        "ALTER TABLE leads ADD COLUMN IF NOT EXISTS company_url TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE leads ADD COLUMN IF NOT EXISTS verified_email TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE leads ADD COLUMN IF NOT EXISTS service_reason TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE leads ADD COLUMN IF NOT EXISTS outreach_status TEXT NOT NULL DEFAULT 'pending'",
        "ALTER TABLE leads ADD COLUMN IF NOT EXISTS followup_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE leads ADD COLUMN IF NOT EXISTS reply_status TEXT NOT NULL DEFAULT 'no_reply'",
        "ALTER TABLE leads ADD COLUMN IF NOT EXISTS last_reply_at TIMESTAMPTZ",
        "UPDATE leads SET company_url = COALESCE(NULLIF(company_url, ''), NULLIF(website, ''), '') WHERE company_url = ''",
        "UPDATE leads SET verified_email = COALESCE(NULLIF(verified_email, ''), NULLIF(email, ''), '') WHERE verified_email = ''",
        service_reason_backfill_sql(),
        "UPDATE leads SET outreach_status = COALESCE(NULLIF(outreach_status, ''), NULLIF(status, ''), 'pending') WHERE outreach_status = ''",
        "UPDATE leads SET country = COALESCE(NULLIF(country, ''), NULLIF(location, ''), '') WHERE country = ''",
        "ALTER TABLE payments ADD COLUMN IF NOT EXISTS user_id TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE payments ADD COLUMN IF NOT EXISTS user_email TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE payments ADD COLUMN IF NOT EXISTS full_name TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE payments ADD COLUMN IF NOT EXISTS phone_number TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE payments ADD COLUMN IF NOT EXISTS whatsapp_number TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE payments ADD COLUMN IF NOT EXISTS currency TEXT NOT NULL DEFAULT 'PKR'",
        "ALTER TABLE payments ADD COLUMN IF NOT EXISTS payment_method TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE payments ADD COLUMN IF NOT EXISTS transaction_reference TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE payments ADD COLUMN IF NOT EXISTS user_note TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE payments ADD COLUMN IF NOT EXISTS admin_note TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE payments ADD COLUMN IF NOT EXISTS reviewed_by TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE payments ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMPTZ",
        "ALTER TABLE payments ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ",
        "ALTER TABLE payments ADD COLUMN IF NOT EXISTS rejected_at TIMESTAMPTZ",
    ]
    for statement in statements:
        await connection.execute(text(statement))


class _AsyncRepositoryProxy:
    def __init__(self, repository_name: str) -> None:
        self.repository_name = repository_name

    async def _run(self, action: Callable[[Any], Awaitable[T]], *, commit: bool = False) -> T:
        session_factory = get_async_session_factory()
        async with session_factory() as session:
            repositories = build_async_repositories(session)
            repository = repositories[self.repository_name]
            result = await action(repository)
            if commit:
                await session.commit()
            return result

    async def list(self, tenant_id: str):
        return await self._run(lambda repository: repository.list(tenant_id))

    async def list_all(self):
        return await self._run(lambda repository: repository.list_all())

    async def get(self, tenant_id: str, item_id: str):
        return await self._run(lambda repository: repository.get(tenant_id, item_id))

    async def save(self, item):
        return await self._run(lambda repository: repository.save(item), commit=True)

    async def delete(self, tenant_id: str, item_id: str):
        return await self._run(lambda repository: repository.delete(tenant_id, item_id), commit=True)


class _AsyncLeadRepositoryProxy(_AsyncRepositoryProxy):
    async def find_by_company_url(self, tenant_id: str, company_url: str):
        return await self._run(lambda repository: repository.find_by_company_url(tenant_id, company_url))

    async def find_by_email(self, tenant_id: str, email: str):
        return await self._run(lambda repository: repository.find_by_email(tenant_id, email))

    async def bulk_save(self, leads: Iterable[Any]) -> Sequence[Any]:
        return await self._run(lambda repository: repository.bulk_save(leads), commit=True)


class _AsyncUserRepositoryProxy(_AsyncRepositoryProxy):
    async def find_by_email(self, tenant_id: str, email: str):
        return await self._run(lambda repository: repository.find_by_email(tenant_id, email))


class _AsyncEmailRepositoryProxy(_AsyncRepositoryProxy):
    async def list_for_lead(self, tenant_id: str, lead_id: str):
        return await self._run(lambda repository: repository.list_for_lead(tenant_id, lead_id))


class _AsyncReplyRepositoryProxy(_AsyncRepositoryProxy):
    async def list_for_lead(self, tenant_id: str, lead_id: str):
        return await self._run(lambda repository: repository.list_for_lead(tenant_id, lead_id))


class _AsyncFollowupRepositoryProxy(_AsyncRepositoryProxy):
    async def list_for_lead(self, tenant_id: str, lead_id: str):
        return await self._run(lambda repository: repository.list_for_lead(tenant_id, lead_id))


class _AsyncJobRepositoryProxy(_AsyncRepositoryProxy):
    async def next_queued(self, queue: str):
        return await self._run(lambda repository: repository.next_queued(queue))

    async def next_queued_for_tenant(self, tenant_id: str, queue: str):
        return await self._run(lambda repository: repository.next_queued_for_tenant(tenant_id, queue))

    async def claim_next_for_tenant(self, tenant_id: str, queue: str, worker_id: str):
        return await self._run(lambda repository: repository.claim_next_for_tenant(tenant_id, queue, worker_id), commit=True)

    async def claim_latest_matching_for_tenant(self, tenant_id: str, queue: str, worker_id: str, job_type: str):
        return await self._run(
            lambda repository: repository.claim_latest_matching_for_tenant(tenant_id, queue, worker_id, job_type),
            commit=True,
        )

    async def get_any(self, item_id: str):
        return await self._run(lambda repository: repository.get_any(item_id))


class _AsyncPaymentRepositoryProxy(_AsyncRepositoryProxy):
    async def find_by_reference(self, tenant_id: str, payment_reference_id: str):
        return await self._run(lambda repository: repository.find_by_reference(tenant_id, payment_reference_id))


def build_postgres_session() -> DatabaseSession:
    return DatabaseSession(
        tenants=_AsyncRepositoryProxy("tenants"),
        users=_AsyncUserRepositoryProxy("users"),
        campaigns=_AsyncRepositoryProxy("campaigns"),
        leads=_AsyncLeadRepositoryProxy("leads"),
        emails=_AsyncEmailRepositoryProxy("emails"),
        replies=_AsyncReplyRepositoryProxy("replies"),
        voice_calls=_AsyncRepositoryProxy("voice_calls"),
        followups=_AsyncFollowupRepositoryProxy("followups"),
        agent_runs=_AsyncRepositoryProxy("agent_runs"),
        jobs=_AsyncJobRepositoryProxy("jobs"),
        payments=_AsyncPaymentRepositoryProxy("payments"),
        gmail_credentials=_AsyncRepositoryProxy("gmail_credentials"),
    )


async def initialize_async_database() -> None:
    engine = get_async_engine()
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await apply_additive_lead_migration(connection)


async def verify_async_database() -> dict[str, Any]:
    engine = get_async_engine()
    async with engine.connect() as connection:
        result = await connection.execute(text("SELECT 1"))
        return {"ok": bool(result.scalar_one() == 1), "backend": "postgres"}
