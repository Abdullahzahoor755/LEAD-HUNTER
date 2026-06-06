import asyncio

from app.db.postgres import initialize_async_database
from app.db.session import get_async_db_session
from app.services.admin_bootstrap_service import ensure_admin_from_env


async def main():
    await initialize_async_database()
    async with get_async_db_session() as db:
        result = await ensure_admin_from_env(db)
        print(
            "Admin ensured: "
            f"email={result.email}, "
            f"tenant_id={result.tenant_id}, "
            f"role={result.role}, "
            f"is_active={result.is_active}"
        )


if __name__ == "__main__":
    asyncio.run(main())
