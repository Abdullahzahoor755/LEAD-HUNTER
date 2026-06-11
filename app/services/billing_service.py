"""Manual subscription billing service."""

from __future__ import annotations

from pathlib import Path
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, Sequence

from app.configs.settings import settings
from app.core.auth import PLAN_LIMITS, normalize_subscription_plan
from app.core.models import Payment, TenantContext
from app.db.session import AsyncDatabaseSession, DatabaseSession
from app.services._async import maybe_await


class BillingService:
    def __init__(self, db: DatabaseSession | AsyncDatabaseSession) -> None:
        self.db = db

    def plans(self) -> Dict[str, Any]:
        return {
            "currency": settings.payment_currency or "PKR",
            "payment": self.payment_instructions(),
            "items": [
                {
                    "name": name,
                    "price": int(settings.billing_plan_prices.get(name, 0) or 0),
                    "limits": limits,
                    "agency_mode": name == "Agency",
                    "qr_path": self.plan_qr_path(name),
                }
                for name, limits in PLAN_LIMITS.items()
            ],
        }

    def plan_qr_path(self, plan: str) -> str:
        normalized = str(plan or "").strip().lower()
        if normalized == "pro":
            return settings.payment_qr_path_pro
        if normalized == "agency":
            return settings.payment_qr_path_agency
        return settings.payment_qr_path

    def payment_instructions(self) -> Dict[str, str]:
        return {
            "account_title": settings.payment_account_title or settings.billing_nayapay_name,
            "account_number": settings.payment_account_number or settings.billing_nayapay_account,
            "method_name": settings.payment_method_name,
            "currency": settings.payment_currency or "PKR",
            "qr_path": settings.payment_qr_path,
            "qr_code_url": settings.billing_qr_code_url,
        }

    async def create_payment_request(
        self,
        tenant: TenantContext,
        *,
        user_email: str,
        full_name: str,
        phone_number: str,
        selected_plan: str,
        payment_method: str,
        transaction_reference: str = "",
        screenshot_path: str,
        user_note: str = "",
    ) -> Payment:
        normalized_plan = normalize_subscription_plan(selected_plan)
        if normalized_plan == "Free":
            raise ValueError("Free plan does not require a payment request.")
        clean_phone = str(phone_number or "").strip()
        if len(clean_phone) < 7 or len(clean_phone) > 32:
            raise ValueError("A valid phone or WhatsApp number is required.")
        if not screenshot_path:
            raise ValueError("Payment screenshot is required.")
        existing = await maybe_await(self.db.payments.list(tenant.tenant_id))
        for payment in existing:
            if payment.plan == normalized_plan and str(payment.status).lower() in {"pending", "needs_review", "pending_verification"}:
                raise ValueError("A pending payment request for this plan already exists.")
        amount = int(settings.billing_plan_prices.get(normalized_plan, 0) or 0)
        payment = Payment(
            tenant_id=tenant.tenant_id,
            user_id=tenant.user_id,
            user_email=str(user_email or tenant.metadata.get("email", "") or "").strip().lower(),
            full_name=str(full_name or "").strip()[:255],
            phone_number=clean_phone,
            whatsapp_number=clean_phone,
            plan=normalized_plan,
            amount=amount,
            currency=settings.payment_currency or "PKR",
            status="pending",
            payment_method=str(payment_method or settings.payment_method_name or "").strip()[:128],
            payment_reference_id=f"{tenant.tenant_id}-{secrets.token_hex(6)}",
            transaction_reference=str(transaction_reference or "").strip()[:255],
            proof_url=screenshot_path,
            user_note=str(user_note or "").strip()[:1000],
        )
        return await maybe_await(self.db.for_tenant(tenant).save("payments", payment))

    async def list_payment_requests(self, tenant: TenantContext) -> Sequence[Payment]:
        return await maybe_await(self.db.payments.list(tenant.tenant_id))

    async def get_payment_request(self, tenant: TenantContext, payment_id: str) -> Payment:
        payment = await maybe_await(self.db.payments.get(tenant.tenant_id, payment_id))
        if payment is None:
            raise ValueError("Payment request not found.")
        return payment

    async def list_admin_payment_requests(self) -> Sequence[Payment]:
        return await maybe_await(self.db.payments.list_all())

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

    async def review_payment_request(self, payment_id: str, status: str, admin: TenantContext, admin_note: str = "") -> Payment:
        normalized_status = str(status or "").strip().lower()
        if normalized_status not in {"approved", "rejected", "needs_review"}:
            raise ValueError("Invalid payment review status.")
        payments: Sequence[Payment] = await maybe_await(self.db.payments.list_all())
        payment = next((item for item in payments if item.id == payment_id), None)
        if payment is None:
            raise ValueError("Payment request not found.")
        current_status = str(payment.status or "").strip().lower()
        if current_status not in {"pending", "needs_review", "pending_verification"}:
            raise ValueError("Payment request has already been reviewed.")
        now = datetime.now(timezone.utc)
        payment.status = normalized_status
        payment.admin_note = str(admin_note or "").strip()[:1000]
        payment.reviewed_by = admin.user_id
        payment.reviewed_at = now
        if normalized_status == "approved":
            payment.approved_at = now
            tenant_records = await maybe_await(self.db.tenants.list(payment.tenant_id))
            if not tenant_records:
                raise ValueError("Tenant not found.")
            tenant_record = tenant_records[0]
            tenant_record.subscription_plan = normalize_subscription_plan(payment.plan)
            tenant_record.subscription_status = "active"
            tenant_record.is_active = True
            await maybe_await(self.db.tenants.save(tenant_record))
        elif normalized_status == "rejected":
            payment.rejected_at = now
        return await maybe_await(self.db.for_tenant(TenantContext(tenant_id=payment.tenant_id)).save("payments", payment))

    def proof_storage_path(self, tenant: TenantContext, reference_id: str, filename: str) -> Path:
        safe_name = f"{secrets.token_hex(16)}{Path(filename or 'proof.png').suffix.lower()}"
        directory = Path("uploads") / "payment_screenshots" / tenant.tenant_id
        directory.mkdir(parents=True, exist_ok=True)
        return directory / safe_name
