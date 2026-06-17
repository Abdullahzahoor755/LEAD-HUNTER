"""Public trust pages for unauthenticated users and OAuth reviewers."""

from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Iterable

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.configs.settings import settings


router = APIRouter()

PRODUCT_NAME = "Lead Hunter AI"
SUPPORT_EMAIL = "abdullahzahoorsdk130@gmail.com"
SUPPORT_PHONE = "03180745230"
PUBLIC_STYLES_PATH = "/public/homepage.css"


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


def _frontend_url() -> str:
    return str(settings.frontend_base_url or "").strip()


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


def _render_homepage() -> HTMLResponse:
    app_url = "/app"
    sections = {
        "preview": (
            ("Lead Generation", "Find prospects by niche and location, then organize them into a workspace-ready pipeline."),
            ("Email CRM", "Review contacts, readiness, outreach state, follow-ups, and reply status from one place."),
            ("Gmail Outreach", "Connect Gmail only when you want to send user-authorized outreach and check replies."),
            ("WhatsApp CRM", "Work phone-ready leads manually with WhatsApp-first status tracking and scripts."),
            ("Marketing Kit", "Turn leads or business ideas into offers, ad copy, scripts, and campaign plans."),
        ),
        "steps": (
            ("Generate Leads", "Search for companies that match your niche, market, and service offer."),
            ("Review Contacts", "Filter email-ready and phone-ready leads before starting outreach."),
            ("Send Outreach", "Use connected Gmail for user-authorized email outreach when you choose."),
            ("Track Replies", "Monitor reply status so your CRM stays focused on real opportunities."),
            ("Grow Faster", "Create sales scripts, offers, and campaign assets from your best leads."),
        ),
        "pricing": (
            ("Free", "$0", "Start with lead workflow and CSV-ready contact management.", "Lead generation workflow, basic CRM views, CSV export"),
            ("Pro", "$20/month", "Add Gmail outreach and reply tracking for active sales work.", "Email CRM, Gmail automation, follow-ups, reply checking"),
            ("Agency", "$30/month", "Use higher limits and agency workflow tools for larger pipelines.", "Everything in Pro, higher limits, agency operating tools"),
        ),
        "trust": (
            ("Gmail Optional", "Create an account and use core features without connecting Gmail."),
            ("Google Login Separate", "Google sign-in uses basic profile scopes and does not grant Gmail access."),
            ("Encrypted Credentials", "Connected provider credentials are stored securely and are not exposed in status APIs."),
            ("Tenant Isolation", "Workspace data is scoped by tenant-aware access controls."),
            ("Disconnect Anytime", "Users can disconnect Gmail in Settings or revoke access from Google."),
        ),
    }
    preview_cards = "\n".join(
        f'<article class="lh-card"><h3>{html.escape(title)}</h3><p>{html.escape(body)}</p></article>'
        for title, body in sections["preview"]
    )
    steps = "\n".join(
        (
            '<li class="lh-step">'
            f'<span class="lh-step-index">{index}</span>'
            f'<div><h3>{html.escape(title)}</h3><p>{html.escape(body)}</p></div>'
            "</li>"
        )
        for index, (title, body) in enumerate(sections["steps"], start=1)
    )
    pricing_cards = "\n".join(
        (
            '<article class="lh-price-card">'
            f'<h3>{html.escape(name)}</h3>'
            f'<p class="lh-price">{html.escape(price)}</p>'
            f'<p>{html.escape(description)}</p>'
            f'<p class="lh-includes">{html.escape(includes)}</p>'
            "</article>"
        )
        for name, price, description, includes in sections["pricing"]
    )
    trust_cards = "\n".join(
        f'<article class="lh-trust-card"><h3>{html.escape(title)}</h3><p>{html.escape(body)}</p></article>'
        for title, body in sections["trust"]
    )
    body = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{PRODUCT_NAME} | Lead generation and Gmail outreach CRM</title>
  <meta name="description" content="Find leads, send Gmail outreach, track replies, and create marketing assets from one Lead Hunter AI workspace.">
  <link rel="stylesheet" href="{PUBLIC_STYLES_PATH}">
</head>
<body class="lh-page">
  <header class="lh-header">
    <a class="lh-brand" href="/" aria-label="{PRODUCT_NAME} home">{PRODUCT_NAME}</a>
    <nav class="lh-nav" aria-label="Public pages">
      <a href="/privacy">Privacy</a>
      <a href="/terms">Terms</a>
      <a href="/contact">Contact</a>
      <a href="/gmail-access">Gmail Access</a>
    </nav>
  </header>
  <main>
    <section class="lh-hero" aria-labelledby="hero-title">
      <div class="lh-hero-copy">
        <p class="lh-eyebrow">AI sales workspace for agencies and growing businesses</p>
        <h1 id="hero-title">Find leads, send Gmail outreach, and track replies.</h1>
        <p class="lh-subheadline">Lead Hunter AI helps you discover prospects, manage email-ready and phone-ready contacts, send user-authorized Gmail outreach, and create marketing assets from one focused workspace.</p>
        <div class="lh-actions">
          <a class="lh-button lh-button-primary" href="{app_url}">Open App</a>
          <a class="lh-button lh-button-secondary" href="/gmail-access">Gmail Access</a>
        </div>
        <p class="lh-hero-note">Gmail connection is optional. Google Login is separate and does not grant Gmail access.</p>
      </div>
      <aside class="lh-preview-panel" aria-label="Product preview">
        <div class="lh-window-bar"><span></span><span></span><span></span></div>
        <div class="lh-preview-row"><strong>Generate Leads</strong><span>niche + location</span></div>
        <div class="lh-preview-row"><strong>Email CRM</strong><span>review contacts</span></div>
        <div class="lh-preview-row"><strong>Gmail Outreach</strong><span>optional connect</span></div>
        <div class="lh-preview-row"><strong>Reply Tracking</strong><span>CRM status</span></div>
        <div class="lh-preview-row"><strong>Marketing Kit</strong><span>offers + scripts</span></div>
      </aside>
    </section>

    <section class="lh-section" aria-labelledby="preview-title">
      <div class="lh-section-heading">
        <p class="lh-eyebrow">Product Preview</p>
        <h2 id="preview-title">One workspace for lead generation and outreach momentum.</h2>
      </div>
      <div class="lh-grid lh-grid-five">{preview_cards}</div>
    </section>

    <section class="lh-section" aria-labelledby="workflow-title">
      <div class="lh-section-heading">
        <p class="lh-eyebrow">How It Works</p>
        <h2 id="workflow-title">Move from search to reply tracking without losing context.</h2>
      </div>
      <ol class="lh-steps">{steps}</ol>
    </section>

    <section class="lh-section" aria-labelledby="pricing-title">
      <div class="lh-section-heading">
        <p class="lh-eyebrow">Pricing Preview</p>
        <h2 id="pricing-title">Start free, then add outreach automation when you need it.</h2>
      </div>
      <div class="lh-pricing">{pricing_cards}</div>
    </section>

    <section class="lh-section lh-trust-section" aria-labelledby="trust-title">
      <div class="lh-section-heading">
        <p class="lh-eyebrow">Trust And Control</p>
        <h2 id="trust-title">Built for safer Gmail-connected workflows.</h2>
      </div>
      <div class="lh-grid lh-grid-five">{trust_cards}</div>
    </section>
  </main>
  <footer class="lh-footer">
    <p>{PRODUCT_NAME} &copy; 2026</p>
    <nav aria-label="Footer">
      <a href="/privacy">Privacy</a>
      <a href="/terms">Terms</a>
      <a href="/contact">Contact</a>
      <a href="/gmail-access">Gmail Access</a>
    </nav>
  </footer>
</body>
</html>"""
    return HTMLResponse(body)


HOMEPAGE_CSS = """
:root {
  color-scheme: light;
  --bg: #f6fbfd;
  --surface: #ffffff;
  --surface-soft: #eff9fc;
  --ink: #102033;
  --muted: #52677a;
  --line: #d7e9ef;
  --line-strong: #b9d9e3;
  --accent: #0e7490;
  --accent-strong: #155e75;
  --accent-soft: #e6f7fb;
  --danger-soft: #fff1f3;
  --shadow: 0 18px 50px rgba(16, 32, 51, .09);
}
* { box-sizing: border-box; }
body.lh-page {
  margin: 0;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  line-height: 1.5;
  color: var(--ink);
  background: linear-gradient(180deg, #edf9fd 0, var(--bg) 380px, #ffffff 100%);
}
a {
  color: var(--accent-strong);
  text-decoration: none;
}
a:hover { text-decoration: underline; }
a:focus-visible {
  outline: 3px solid rgba(14, 116, 144, .35);
  outline-offset: 4px;
  border-radius: 8px;
}
.lh-header,
.lh-hero,
.lh-section,
.lh-footer {
  width: min(1160px, calc(100% - 40px));
  margin-left: auto;
  margin-right: auto;
}
.lh-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
  padding: 24px 0;
}
.lh-brand {
  font-weight: 850;
  color: var(--ink);
  font-size: 1.05rem;
}
.lh-nav,
.lh-footer nav {
  display: flex;
  align-items: center;
  gap: 18px;
  flex-wrap: wrap;
  color: var(--muted);
  font-size: .95rem;
}
.lh-hero {
  display: grid;
  grid-template-columns: minmax(0, 1.05fr) minmax(320px, .72fr);
  gap: 42px;
  align-items: center;
  padding: 66px 0 58px;
}
.lh-eyebrow {
  margin: 0 0 14px;
  color: var(--accent-strong);
  font-size: .84rem;
  font-weight: 800;
  letter-spacing: .08em;
  text-transform: uppercase;
}
h1,
h2,
h3,
p {
  overflow-wrap: anywhere;
}
h1 {
  margin: 0;
  max-width: 820px;
  font-size: clamp(2.45rem, 6vw, 5rem);
  line-height: .96;
  letter-spacing: 0;
}
h2 {
  margin: 0;
  font-size: clamp(1.65rem, 3.4vw, 2.7rem);
  line-height: 1.06;
  letter-spacing: 0;
}
h3 {
  margin: 0 0 8px;
  font-size: 1.02rem;
  line-height: 1.25;
}
.lh-subheadline {
  max-width: 740px;
  margin: 24px 0 0;
  color: var(--muted);
  font-size: 1.16rem;
}
.lh-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  margin-top: 30px;
}
.lh-button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 46px;
  padding: 0 20px;
  border-radius: 8px;
  border: 1px solid var(--line-strong);
  font-weight: 800;
}
.lh-button-primary {
  color: #ffffff;
  background: var(--accent-strong);
  border-color: var(--accent-strong);
  box-shadow: 0 12px 28px rgba(21, 94, 117, .2);
}
.lh-button-primary:hover {
  color: #ffffff;
  text-decoration: none;
  background: #0f4d61;
}
.lh-button-secondary {
  background: #ffffff;
  color: var(--accent-strong);
}
.lh-button-secondary:hover { text-decoration: none; background: var(--accent-soft); }
.lh-hero-note {
  margin: 18px 0 0;
  color: var(--muted);
  font-size: .95rem;
}
.lh-preview-panel {
  border: 1px solid var(--line);
  border-radius: 14px;
  background: rgba(255, 255, 255, .86);
  box-shadow: var(--shadow);
  padding: 18px;
}
.lh-window-bar {
  display: flex;
  gap: 7px;
  padding-bottom: 16px;
  border-bottom: 1px solid var(--line);
}
.lh-window-bar span {
  width: 10px;
  height: 10px;
  border-radius: 999px;
  background: var(--line-strong);
}
.lh-preview-row {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  padding: 16px 0;
  border-bottom: 1px solid #e9f3f6;
}
.lh-preview-row:last-child { border-bottom: 0; }
.lh-preview-row span {
  color: var(--muted);
  text-align: right;
}
.lh-section {
  padding: 56px 0;
}
.lh-section-heading {
  max-width: 760px;
  margin-bottom: 24px;
}
.lh-grid {
  display: grid;
  gap: 16px;
}
.lh-grid-five {
  grid-template-columns: repeat(5, minmax(0, 1fr));
}
.lh-card,
.lh-trust-card,
.lh-price-card,
.lh-step {
  border: 1px solid var(--line);
  border-radius: 10px;
  background: var(--surface);
  box-shadow: 0 10px 28px rgba(16, 32, 51, .045);
}
.lh-card,
.lh-trust-card,
.lh-price-card {
  padding: 20px;
}
.lh-card p,
.lh-trust-card p,
.lh-price-card p,
.lh-step p {
  margin: 0;
  color: var(--muted);
}
.lh-steps {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 16px;
  list-style: none;
  margin: 0;
  padding: 0;
}
.lh-step {
  padding: 18px;
}
.lh-step-index {
  display: inline-grid;
  place-items: center;
  width: 32px;
  height: 32px;
  margin-bottom: 14px;
  border-radius: 8px;
  background: var(--accent-soft);
  color: var(--accent-strong);
  font-weight: 850;
}
.lh-pricing {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 16px;
}
.lh-price {
  margin: 4px 0 12px !important;
  color: var(--ink) !important;
  font-size: 1.7rem;
  font-weight: 850;
}
.lh-includes {
  margin-top: 16px !important;
  padding-top: 14px;
  border-top: 1px solid var(--line);
  font-size: .93rem;
}
.lh-trust-section {
  margin-top: 24px;
  padding: 46px 28px;
  border-radius: 18px;
  background: linear-gradient(135deg, var(--surface-soft), #ffffff);
  border: 1px solid var(--line);
}
.lh-footer {
  display: flex;
  justify-content: space-between;
  gap: 20px;
  align-items: center;
  padding: 32px 0 42px;
  margin-top: 32px;
  border-top: 1px solid var(--line);
  color: var(--muted);
}
.lh-footer p { margin: 0; }
@media (max-width: 1040px) {
  .lh-grid-five,
  .lh-steps {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .lh-hero {
    grid-template-columns: 1fr;
    padding-top: 42px;
  }
}
@media (max-width: 720px) {
  .lh-header,
  .lh-footer {
    align-items: flex-start;
    flex-direction: column;
  }
  .lh-header,
  .lh-hero,
  .lh-section,
  .lh-footer {
    width: min(100% - 28px, 1160px);
  }
  .lh-hero {
    gap: 26px;
    padding: 32px 0 38px;
  }
  .lh-section {
    padding: 38px 0;
  }
  .lh-grid-five,
  .lh-steps,
  .lh-pricing {
    grid-template-columns: 1fr;
  }
  .lh-actions,
  .lh-button {
    width: 100%;
  }
  .lh-preview-row {
    flex-direction: column;
    gap: 4px;
  }
  .lh-preview-row span {
    text-align: left;
  }
  .lh-trust-section {
    padding: 30px 16px;
  }
}
"""


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
                f"For support, contact {SUPPORT_EMAIL} or {SUPPORT_PHONE}.",
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
                f"To request support, access, correction, or deletion, contact {SUPPORT_EMAIL} or {SUPPORT_PHONE}.",
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
                f"Questions about these terms can be sent to {SUPPORT_EMAIL} or {SUPPORT_PHONE}.",
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
                f"For account, billing, privacy, Gmail access, or technical support, contact {SUPPORT_EMAIL} or {SUPPORT_PHONE}.",
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
                f"For help with Gmail access, contact {SUPPORT_EMAIL} or {SUPPORT_PHONE}.",
            ),
        ),
    ),
)


@router.get("/", response_class=HTMLResponse)
async def home() -> HTMLResponse:
    return _render_homepage()


@router.get("/app")
async def open_app() -> Response:
    frontend_url = _frontend_url()
    if not frontend_url or frontend_url == "/":
        return HTMLResponse(
            "<!doctype html><html><body><h1>App URL is not configured</h1>"
            "<p>Set FRONTEND_BASE_URL to the Streamlit app URL, then reload this page.</p>"
            '<p><a href="/">Back to homepage</a></p></body></html>',
            status_code=503,
        )
    return RedirectResponse(frontend_url, status_code=302)


@router.get(PUBLIC_STYLES_PATH)
async def homepage_styles() -> Response:
    return Response(HOMEPAGE_CSS, media_type="text/css")


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
