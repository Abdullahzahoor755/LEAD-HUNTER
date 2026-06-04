"""Manual subscription billing service."""

from __future__ import annotations

from pathlib import Path
import secrets

from app.configs.settings import settings
from app.core.auth import normalize_subscription_plan
from app.core.models import Payment, TenantContext
from app.db.session import AsyncDatabaseSession, DatabaseSession
from app.services._async import maybe_await


class BillingService:
    def __init__(self, db: DatabaseSession | AsyncDatabaseSession) -> None:
        self.db = db

    async def subscribe(self, tenant: TenantContext, plan: str) -> dict:
        normalized_plan = normalize_subscription_plan(plan)
        amount = int(settings.billing_plan_prices[normalized_plan])
        reference_id = f"{tenant.tenant_id}-{secrets.token_hex(6)}"
        payment = Payment(
            tenant_id=tenant.tenant_id,
            plan=normalized_plan,
            amount=amount,
            status="pending",
            payment_reference_id=reference_id,
        )
        await maybe_await(self.db.for_tenant(tenant).save("payments", payment))
        return {
            "payment_reference_id": reference_id,
            "plan_name": normalized_plan,
            "price": amount,
            "payment_instructions": {
                "nayapay": {
                    "account_name": settings.billing_nayapay_name,
                    "account_id": settings.billing_nayapay_account,
                },
                "sadapay": {
                    "account_name": settings.billing_sadapay_name,
                    "account_id": settings.billing_sadapay_account,
                },
            },
            "qr_code_url": settings.billing_qr_code_url,
            "next_steps": [
                "Transfer the exact amount to Nayapay or Sadapay.",
                "Include the payment reference ID in your payment note if possible.",
                "Upload your payment proof using /billing/upload-proof.",
                "Wait for manual verification from admin.",
            ],
        }

    async def upload_proof(self, tenant: TenantContext, reference_id: str, proof_url: str) -> Payment:
        payment = await maybe_await(self.db.payments.find_by_reference(tenant.tenant_id, reference_id))
        if payment is None:
            raise ValueError("Payment reference not found.")
        payment.proof_url = proof_url
        payment.status = "pending_verification"
        return await maybe_await(self.db.for_tenant(tenant).save("payments", payment))

    async def approve_payment(self, tenant: TenantContext, reference_id: str) -> Payment:
        payment = await maybe_await(self.db.payments.find_by_reference(tenant.tenant_id, reference_id))
        if payment is None:
            raise ValueError("Payment reference not found.")
        payment.status = "verified"
        await maybe_await(self.db.for_tenant(tenant).save("payments", payment))
        tenant_records = await maybe_await(self.db.tenants.list(tenant.tenant_id))
        if not tenant_records:
            raise ValueError("Tenant not found.")
        tenant_record = tenant_records[0]
        tenant_record.is_active = True
        tenant_record.subscription_status = "active"
        tenant_record.subscription_plan = payment.plan
        await maybe_await(self.db.tenants.save(tenant_record))
        return payment

    def proof_storage_path(self, tenant: TenantContext, reference_id: str, filename: str) -> Path:
        safe_name = Path(filename or "proof.bin").name
        directory = settings.data_dir / "payment_proofs" / tenant.tenant_id
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{reference_id}-{safe_name}"
