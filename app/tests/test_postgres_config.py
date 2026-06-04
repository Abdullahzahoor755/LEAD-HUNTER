from __future__ import annotations

from app.db.postgres import looks_like_score_breakdown_reason, service_reason_backfill_sql
from app.db.session import normalize_async_database_url


def test_normalize_async_database_url_keeps_asyncpg_url() -> None:
    url = "postgresql+asyncpg://postgres:postgres@localhost:5432/lead_generator"
    assert normalize_async_database_url(url) == url


def test_normalize_async_database_url_upgrades_postgresql_scheme() -> None:
    url = "postgresql://postgres:postgres@localhost:5432/lead_generator"
    assert normalize_async_database_url(url) == "postgresql+asyncpg://postgres:postgres@localhost:5432/lead_generator"


def test_normalize_async_database_url_upgrades_postgres_scheme() -> None:
    url = "postgres://postgres:postgres@localhost:5432/lead_generator"
    assert normalize_async_database_url(url) == "postgresql+asyncpg://postgres:postgres@localhost:5432/lead_generator"


def test_migration_does_not_backfill_score_breakdown_reason() -> None:
    reason = "email=40/40 | phone=25/25 | relevance=0/20 | quality=8/10"

    assert looks_like_score_breakdown_reason(reason)
    sql = service_reason_backfill_sql().lower()
    assert "set service_reason = nullif(reason, '')" in sql
    assert "and not" in sql
    assert "email=%/%" in sql
    assert "phone=%/%" in sql
    assert "relevance=%" in sql
    assert "quality=%" in sql


def test_migration_can_backfill_real_business_reason() -> None:
    reason = "The company shows demand for business workflow automation."

    assert not looks_like_score_breakdown_reason(reason)
