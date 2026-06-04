import asyncio
from app.db.postgres import initialize_async_database
from app.db.session import get_async_db_session
from app.services.auth_service import AuthService
from app.services._async import maybe_await

async def main():
    await initialize_async_database()
    async with get_async_db_session() as db:
        auth_service = AuthService(db)
        
        # Check if user already exists
        existing = await maybe_await(db.users.find_by_email("Tenant-id1", "abdullahzahoorsdk139@gmail.com"))
        if existing:
            print("User already exists!")
            # Optionally update password here if needed, but signup is simpler if not exists
            return

        try:
            result = await auth_service.signup(
                tenant_id="Tenant-id1",
                tenant_name="Admin Tenant",
                tenant_slug="admin",
                email="abdullahzahoorsdk139@gmail.com",
                password="78563214",
                full_name="Admin User",
                plan="Agency",
                role="admin"
            )
            print("Admin user seeded successfully!")
        except Exception as e:
            print(f"Failed to create: {e}")

if __name__ == "__main__":
    asyncio.run(main())
