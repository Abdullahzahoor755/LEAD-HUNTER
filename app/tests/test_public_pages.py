from __future__ import annotations

import httpx
import pytest

from app.api.app import create_fastapi_app
from app.db.session import build_memory_session


PUBLIC_PATHS = ("/", "/privacy", "/terms", "/contact", "/gmail-access")


@pytest.mark.anyio
async def test_public_trust_pages_are_available_without_auth() -> None:
    app = create_fastapi_app(db=build_memory_session())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        for path in PUBLIC_PATHS:
            response = await client.get(path)
            assert response.status_code == 200
            assert "text/html" in response.headers["content-type"]
            assert "Lead Hunter AI" in response.text


@pytest.mark.anyio
async def test_public_pages_include_required_trust_links_and_gmail_disclosure() -> None:
    app = create_fastapi_app(db=build_memory_session())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        privacy = await client.get("/privacy")
        gmail_access = await client.get("/gmail-access")

    assert privacy.status_code == 200
    assert "/privacy" in privacy.text
    assert "/terms" in privacy.text
    assert "/contact" in privacy.text
    assert "/gmail-access" in privacy.text
    assert "gmail.send" in privacy.text
    assert "gmail.readonly" in privacy.text
    assert "Google Login does not grant Gmail access" in privacy.text

    assert gmail_access.status_code == 200
    assert "Gmail Connection Is Optional" in gmail_access.text
    assert "gmail.send" in gmail_access.text
    assert "gmail.readonly" in gmail_access.text
    assert "does not sell Gmail data" in gmail_access.text


@pytest.mark.anyio
async def test_homepage_has_saas_landing_sections() -> None:
    app = create_fastapi_app(db=build_memory_session())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/")
        styles = await client.get("/public/homepage.css")

    assert response.status_code == 200
    assert 'href="/public/homepage.css"' in response.text
    assert 'href="/app"' in response.text
    assert "Find leads, send Gmail outreach, and track replies." in response.text
    assert "Open App" in response.text
    assert "Gmail Access" in response.text
    for section in ("Product Preview", "How It Works", "Pricing Preview", "Trust And Control"):
        assert section in response.text
    for label in (
        "Lead Generation",
        "Email CRM",
        "Gmail Outreach",
        "WhatsApp CRM",
        "Marketing Kit",
        "Generate Leads",
        "Review Contacts",
        "Send Outreach",
        "Track Replies",
        "Grow Faster",
        "Free",
        "Pro",
        "Agency",
        "Gmail Optional",
        "Google Login Separate",
        "Encrypted Credentials",
        "Tenant Isolation",
        "Disconnect Anytime",
    ):
        assert label in response.text

    assert styles.status_code == 200
    assert "text/css" in styles.headers["content-type"]
    assert ".lh-hero" in styles.text
    assert "@media" in styles.text


@pytest.mark.anyio
async def test_open_app_redirects_to_configured_frontend(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api.public_pages import settings

    monkeypatch.setattr(settings, "frontend_base_url", "https://app.leadhunter.test")
    app = create_fastapi_app(db=build_memory_session())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver", follow_redirects=False) as client:
        response = await client.get("/app")

    assert response.status_code == 302
    assert response.headers["location"] == "https://app.leadhunter.test"


@pytest.mark.anyio
async def test_open_app_has_clear_error_when_frontend_url_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api.public_pages import settings

    monkeypatch.setattr(settings, "frontend_base_url", "")
    app = create_fastapi_app(db=build_memory_session())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/app")

    assert response.status_code == 503
    assert "App URL is not configured" in response.text
    assert "FRONTEND_BASE_URL" in response.text


@pytest.mark.anyio
async def test_public_pages_do_not_expose_secret_markers() -> None:
    app = create_fastapi_app(db=build_memory_session())
    transport = httpx.ASGITransport(app=app)
    forbidden = (
        "GOOGLE_OAUTH_CLIENT_SECRET",
        "GOOGLE_AUTH_CLIENT_SECRET",
        "JWT_SECRET",
        "SECRET_ENCRYPTION_KEY",
        "DATABASE_URL",
        "refresh_token",
        "access_token",
        "client_secret",
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        for path in PUBLIC_PATHS:
            response = await client.get(path)
            assert response.status_code == 200
            for marker in forbidden:
                assert marker not in response.text


@pytest.mark.anyio
async def test_sensitive_routes_still_require_auth() -> None:
    app = create_fastapi_app(db=build_memory_session())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        leads = await client.get("/leads")
        gmail_start = await client.get("/settings/providers/gmail/oauth/start")
        gmail_status = await client.get("/settings/providers/gmail/status")
        admin = await client.get("/admin/summary")

    assert leads.status_code == 401
    assert gmail_start.status_code == 401
    assert gmail_status.status_code == 401
    assert admin.status_code == 401
