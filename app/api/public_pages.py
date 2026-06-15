"""Public trust pages for unauthenticated users and OAuth reviewers."""

from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Iterable

from fastapi import APIRouter
from fastapi.responses import HTMLResponse


router = APIRouter()

PRODUCT_NAME = "Lead Hunter AI"
SUPPORT_EMAIL = "support@leadhunterai.app"


@dataclass(frozen=True)
class Page:
    title: str
    description: str
    sections: tuple[tuple[str, tuple[str, ...]], ...]


def _nav() -> str:
    links = (
        ("/", "Home"),
        ("/privacy", "Privacy Policy"),
        ("/terms", "Terms of Service"),
        ("/contact", "Contact"),
        ("/gmail-access", "Gmail Access"),
    )
    return " | ".join(f'<a href="{href}">{html.escape(label)}</a>' for href, label in links)


def _paragraphs(items: Iterable[str]) -> str:
    return "\n".join(f"<p>{html.escape(item)}</p>" for item in items)


def _render_page(page: Page) -> HTMLResponse:
    sections = "\n".join(
        f"<section><h2>{html.escape(title)}</h2>{_paragraphs(body)}</section>"
        for title, body in page.sections
    )
    body = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(page.title)} | {PRODUCT_NAME}</title>
</head>
<body>
  <header>
    <nav>{_nav()}</nav>
    <h1>{html.escape(page.title)}</h1>
    <p>{html.escape(page.description)}</p>
  </header>
  <main>
    {sections}
  </main>
  <footer>
    <p>{PRODUCT_NAME} &copy; 2026. <a href="/privacy">Privacy Policy</a> | <a href="/terms">Terms of Service</a> | <a href="/contact">Contact</a> | <a href="/gmail-access">Gmail Access</a></p>
  </footer>
</body>
</html>"""
    return HTMLResponse(body)


HOME_PAGE = Page(
    title=PRODUCT_NAME,
    description="AI lead generation, outreach CRM, Gmail-connected email workflow, WhatsApp CRM, and marketing tools for agencies and businesses.",
    sections=(
        (
            "What Lead Hunter AI Does",
            (
                "Lead Hunter AI helps users find business leads, manage outreach-ready contacts, create marketing assets, and organize follow-up workflows from one SaaS workspace.",
                "The app includes lead generation, Email CRM, WhatsApp CRM, Marketing Kit, Billing, Settings, and an Admin Panel for authorized administrators.",
            ),
        ),
        (
            "Gmail Is Optional",
            (
                "Users can use Lead Hunter AI without connecting Gmail. Gmail access is only requested when an eligible user chooses to connect Gmail for email outreach and reply tracking.",
                "Google Login is separate from Gmail access and uses only basic sign-in scopes.",
            ),
        ),
        (
            "Trust And Support",
            (
                "Public privacy, terms, contact, and Gmail access details are available from the links on this page.",
                f"For support, contact {SUPPORT_EMAIL}.",
            ),
        ),
    ),
)


PRIVACY_PAGE = Page(
    title="Privacy Policy",
    description="How Lead Hunter AI collects, uses, protects, and handles account, workspace, lead, billing, and Google/Gmail data.",
    sections=(
        (
            "Information We Collect",
            (
                "We may collect account details such as name, email address, workspace or tenant name, login information, billing request details, support messages, and app usage information.",
                "Users may create or store lead records, outreach statuses, generated campaign content, CRM notes, and settings inside their workspace.",
            ),
        ),
        (
            "Google Login",
            (
                "Google Login is used only to create or sign into a Lead Hunter AI workspace. It may use basic profile information such as email address, name, profile image, and Google account identifier.",
                "Google Login does not grant Gmail access and does not request Gmail scopes.",
            ),
        ),
        (
            "Gmail Data",
            (
                "Gmail access is optional and is requested only when a user connects Gmail from Settings for outreach automation and reply tracking.",
                "Lead Hunter AI requests gmail.send to send user-authorized outreach emails and gmail.readonly to check replies so CRM statuses can be updated.",
                "OAuth tokens are stored securely and are not returned by status APIs. Sender email and outreach or reply status may be stored in the user's tenant workspace.",
            ),
        ),
        (
            "Limited Use",
            (
                "Lead Hunter AI uses Google user data only to provide user-facing app features such as sign-in, Gmail outreach, and reply tracking.",
                "We do not sell Gmail data, use Gmail data for advertising, or transfer Gmail data except as necessary to provide the service, comply with law, protect security, or with user direction.",
                "Humans do not read Gmail data except with user permission, for support requested by the user, for security, or where legally required.",
            ),
        ),
        (
            "Security And Tenant Isolation",
            (
                "Lead Hunter AI uses authentication, tenant-aware access controls, and secure provider credential storage to protect workspace data.",
                "No internet service can guarantee perfect security, but we design the app to keep each tenant's data isolated from other tenants.",
            ),
        ),
        (
            "User Controls And Contact",
            (
                "Users can disconnect Gmail in Settings and can revoke access from their Google Account permissions page.",
                f"To request support, access, correction, or deletion, contact {SUPPORT_EMAIL}.",
            ),
        ),
    ),
)


TERMS_PAGE = Page(
    title="Terms of Service",
    description="Rules for using Lead Hunter AI, including account, outreach, Gmail, billing, and acceptable-use responsibilities.",
    sections=(
        (
            "Use Of The Service",
            (
                "Lead Hunter AI provides lead generation, CRM, email outreach, WhatsApp workflow support, marketing content generation, billing, settings, and administration features.",
                "Users are responsible for keeping account credentials secure and for all activity inside their workspace.",
            ),
        ),
        (
            "Outreach Compliance",
            (
                "Users are responsible for ensuring that email, WhatsApp, phone, and marketing outreach complies with applicable laws and platform rules.",
                "Lead Hunter AI does not guarantee deliverability, replies, conversions, revenue, or the accuracy of every generated lead or AI output.",
            ),
        ),
        (
            "Google And Gmail",
            (
                "Gmail connection is optional. When a user connects Gmail, the user authorizes Lead Hunter AI to send outreach emails and check replies for CRM tracking according to the permissions shown by Google.",
                "Users can disconnect Gmail in the app or revoke access from their Google Account permissions.",
            ),
        ),
        (
            "Billing",
            (
                "Paid plan access, payment review, activation, cancellation, or refunds are handled according to the billing flow and support process presented in the app.",
                "Users must provide accurate payment request information when requesting paid access.",
            ),
        ),
        (
            "Acceptable Use",
            (
                "Users may not use Lead Hunter AI for unlawful, deceptive, abusive, harmful, spam-heavy, or unauthorized activity.",
                "Users may not attempt to bypass authentication, billing controls, tenant isolation, rate limits, or security protections.",
            ),
        ),
        (
            "Contact",
            (
                f"Questions about these terms can be sent to {SUPPORT_EMAIL}.",
            ),
        ),
    ),
)


CONTACT_PAGE = Page(
    title="Contact",
    description="Support and contact information for Lead Hunter AI users and OAuth reviewers.",
    sections=(
        (
            "Support",
            (
                f"For account, billing, privacy, Gmail access, or technical support, contact {SUPPORT_EMAIL}.",
                "Please include your workspace name, account email, and a short description of the issue when requesting support.",
            ),
        ),
        (
            "OAuth Reviewers",
            (
                "Lead Hunter AI's public Privacy Policy, Terms of Service, and Gmail Access explanation are linked from this page.",
                "Gmail access is optional and is used only for user-authorized outreach sending and reply tracking.",
            ),
        ),
    ),
)


GMAIL_ACCESS_PAGE = Page(
    title="Gmail Access Transparency",
    description="A clear explanation of why Lead Hunter AI requests Gmail permissions and how users stay in control.",
    sections=(
        (
            "Gmail Connection Is Optional",
            (
                "Users can create an account, log in, generate leads, and use many workspace features without connecting Gmail.",
                "Gmail access is requested only when an eligible user chooses to connect Gmail from Settings for email outreach and reply tracking.",
            ),
        ),
        (
            "Permissions Requested",
            (
                "gmail.send is used to send user-authorized outreach emails from the connected Gmail account.",
                "gmail.readonly is used to check replies so Lead Hunter AI can update CRM statuses such as replied, interested, or not interested.",
            ),
        ),
        (
            "What We Do Not Do",
            (
                "Lead Hunter AI does not sell Gmail data, use Gmail data for advertising, or request Gmail access for basic Google Login.",
                "Lead Hunter AI does not expose Gmail refresh tokens, access tokens, or OAuth client secrets in the dashboard or status APIs.",
            ),
        ),
        (
            "Storage And Controls",
            (
                "OAuth tokens are stored securely, and sender email plus outreach or reply statuses may be stored in the user's tenant workspace.",
                "Users can disconnect Gmail in Settings or revoke access from their Google Account permissions page at any time.",
            ),
        ),
        (
            "Related Policies",
            (
                "Read the Privacy Policy and Terms of Service for more detail about data use, security, acceptable use, and support.",
                f"For help with Gmail access, contact {SUPPORT_EMAIL}.",
            ),
        ),
    ),
)


@router.get("/", response_class=HTMLResponse)
async def home() -> HTMLResponse:
    return _render_page(HOME_PAGE)


@router.get("/privacy", response_class=HTMLResponse)
async def privacy() -> HTMLResponse:
    return _render_page(PRIVACY_PAGE)


@router.get("/terms", response_class=HTMLResponse)
async def terms() -> HTMLResponse:
    return _render_page(TERMS_PAGE)


@router.get("/contact", response_class=HTMLResponse)
async def contact() -> HTMLResponse:
    return _render_page(CONTACT_PAGE)


@router.get("/gmail-access", response_class=HTMLResponse)
async def gmail_access() -> HTMLResponse:
    return _render_page(GMAIL_ACCESS_PAGE)
