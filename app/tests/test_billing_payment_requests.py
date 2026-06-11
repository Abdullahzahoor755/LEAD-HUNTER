from __future__ import annotations

import httpx
import pytest

from app.api.app import create_fastapi_app
from app.core.models import User
from app.db.session import build_memory_session
from pathlib import Path


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _signup(client: httpx.AsyncClient, tenant_name: str, email: str) -> dict[str, str]:
    response = await client.post(
        "/signup",
        json={
            "tenant_name": tenant_name,
            "email": email,
            "password": "secret123",
            "full_name": "Billing User",
        },
    )
    assert response.status_code == 200
    return response.json()


def _png_file(name: str = "proof.png") -> dict[str, tuple[str, bytes, str]]:
    return {"screenshot": (name, b"\x89PNG\r\n\x1a\nproof", "image/png")}


def test_env_example_payment_placeholders_only() -> None:
    source = Path(".env.example").read_text(encoding="utf-8")
    assert "PAYMENT_ACCOUNT_TITLE=replace_me" in source
    assert "PAYMENT_ACCOUNT_NUMBER=replace_me" in source
    assert "PAYMENT_QR_PATH=assets/payment-qr.jpeg" in source
    assert "PAYMENT_QR_PATH_PRO=assets/payment-qr-pro.jpeg" in source
    assert "PAYMENT_QR_PATH_AGENCY=assets/payment-qr-agency.jpeg" in source
    assert "BILLING_PRICE_PRO_PKR=2800" in source
    assert "BILLING_PRICE_AGENCY_PKR=5000" in source


async def _payment_request(client: httpx.AsyncClient, token: str, plan: str = "Pro") -> httpx.Response:
    return await client.post(
        "/billing/payment-requests",
        headers=_auth_headers(token),
        data={
            "full_name": "Billing User",
            "phone_number": "+923001234567",
            "selected_plan": plan,
            "payment_method": "JazzCash",
            "transaction_reference": "TX-123",
            "user_note": "Please review.",
        },
        files=_png_file(),
    )


async def _make_admin(db, client: httpx.AsyncClient) -> dict[str, str]:
    signup = await _signup(client, "Admin Billing", "admin-billing@example.test")
    user = db.users.find_by_email(signup["tenant_id"], "admin-billing@example.test")
    assert isinstance(user, User)
    user.role = "admin"
    db.for_tenant(signup["tenant_id"]).save("users", user)
    login = await client.post(
        "/login",
        json={"tenant_id": signup["tenant_id"], "email": "admin-billing@example.test", "password": "secret123"},
    )
    assert login.status_code == 200
    return login.json()


@pytest.mark.anyio
async def test_user_can_create_and_list_pending_payment_request() -> None:
    app = create_fastapi_app(db=build_memory_session())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        signup = await _signup(client, "Tenant Billing One", "owner@billing-one.test")
        plans = await client.get("/billing/plans", headers=_auth_headers(signup["token"]))
        assert plans.status_code == 200
        plan_map = {item["name"]: item for item in plans.json()["items"]}
        assert plan_map["Pro"]["price"] == 2800
        assert plan_map["Pro"]["qr_path"] == "assets/payment-qr-pro.jpeg"
        assert plan_map["Agency"]["price"] == 5000
        assert plan_map["Agency"]["qr_path"] == "assets/payment-qr-agency.jpeg"
        response = await _payment_request(client, signup["token"], "Pro")
        assert response.status_code == 200
        payment = response.json()["payment_request"]
        assert payment["status"] == "pending"
        assert payment["plan"] == "Pro"
        assert payment["has_screenshot"] is True

        mine = await client.get("/billing/payment-requests/me", headers=_auth_headers(signup["token"]))
        assert mine.status_code == 200
        assert len(mine.json()["items"]) == 1


@pytest.mark.anyio
async def test_payment_request_validation_and_duplicate_guard() -> None:
    app = create_fastapi_app(db=build_memory_session())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        signup = await _signup(client, "Tenant Billing Validation", "owner@billing-validation.test")
        missing_phone = await client.post(
            "/billing/payment-requests",
            headers=_auth_headers(signup["token"]),
            data={"full_name": "Owner", "phone_number": "", "selected_plan": "Pro", "payment_method": "JazzCash"},
            files=_png_file(),
        )
        assert missing_phone.status_code == 400

        invalid_file = await client.post(
            "/billing/payment-requests",
            headers=_auth_headers(signup["token"]),
            data={"full_name": "Owner", "phone_number": "+923001234567", "selected_plan": "Pro", "payment_method": "JazzCash"},
            files={"screenshot": ("proof.txt", b"not image", "text/plain")},
        )
        assert invalid_file.status_code == 400

        first = await _payment_request(client, signup["token"], "Pro")
        assert first.status_code == 200
        duplicate = await _payment_request(client, signup["token"], "Pro")
        assert duplicate.status_code == 400


@pytest.mark.anyio
async def test_payment_requests_are_tenant_scoped_and_admin_only() -> None:
    db = build_memory_session()
    app = create_fastapi_app(db=db)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        first = await _signup(client, "Tenant Billing Scope One", "owner@billing-scope-one.test")
        second = await _signup(client, "Tenant Billing Scope Two", "owner@billing-scope-two.test")
        admin = await _make_admin(db, client)
        created = await _payment_request(client, first["token"], "Agency")
        payment_id = created.json()["payment_request"]["id"]

        other_lookup = await client.get(f"/billing/payment-requests/{payment_id}", headers=_auth_headers(second["token"]))
        assert other_lookup.status_code == 404

        non_admin = await client.get("/admin/payment-requests", headers=_auth_headers(first["token"]))
        assert non_admin.status_code == 403

        admin_list = await client.get("/admin/payment-requests", headers=_auth_headers(admin["token"]))
        assert admin_list.status_code == 200
        assert any(item["id"] == payment_id for item in admin_list.json()["items"])


@pytest.mark.anyio
async def test_admin_approve_updates_plan_and_reject_keeps_plan() -> None:
    db = build_memory_session()
    app = create_fastapi_app(db=db)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        user = await _signup(client, "Tenant Billing Approve", "owner@billing-approve.test")
        admin = await _make_admin(db, client)
        created = await _payment_request(client, user["token"], "Agency")
        payment_id = created.json()["payment_request"]["id"]

        approved = await client.post(
            f"/admin/payment-requests/{payment_id}/approve",
            headers=_auth_headers(admin["token"]),
            json={"admin_note": "approved"},
        )
        assert approved.status_code == 200
        tenant = db.tenants.list(user["tenant_id"])[0]
        assert tenant.subscription_plan == "Agency"

        other = await _signup(client, "Tenant Billing Reject", "owner@billing-reject.test")
        rejected_request = await _payment_request(client, other["token"], "Pro")
        rejected_id = rejected_request.json()["payment_request"]["id"]
        rejected = await client.post(
            f"/admin/payment-requests/{rejected_id}/reject",
            headers=_auth_headers(admin["token"]),
            json={"admin_note": "not received"},
        )
        assert rejected.status_code == 200
        assert db.tenants.list(other["tenant_id"])[0].subscription_plan == "Free"
