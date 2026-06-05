"""
Streamlit analytics dashboard backed by PostgreSQL.
"""

from __future__ import annotations

from datetime import datetime
import html
import json
import mimetypes
import os
from pathlib import Path
from typing import Any, Dict, List

import httpx
import pandas as pd
import plotly.express as px
import streamlit as st

from app.core.auth import decode_jwt_token
from app.core.models import TenantContext

try:
    from streamlit_autorefresh import st_autorefresh
except ImportError:
    st_autorefresh = None


PRODUCT_NAME = "Lead Hunter AI"
PRODUCT_TAGLINE = "Find business leads in seconds — generate company URLs, emails, phones, and export-ready CSVs."
LOCKED_FEATURE_MESSAGE = "This feature is available in Pro plan. Upgrade to automate outreach, followups, and reply tracking."

st.set_page_config(page_title=PRODUCT_NAME, page_icon="🔎", layout="wide")

API_BASE_URL = os.getenv("APP_API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
DEFAULT_PAYMENT_QR_PATH = "/home/mabdullah/Downloads/WhatsApp Image 2026-05-25 at 12.39.52 AM.jpeg"


def bootstrap_dashboard_state() -> None:
    st.session_state.setdefault("campaigns", [])
    st.session_state.setdefault("auth", {})
    st.session_state.setdefault("latest_subscription", {})
    st.session_state.setdefault("plan_onboarding_seen", "")
    st.session_state.setdefault("lead_generation_busy", False)


PLAN_DETAILS: Dict[str, Dict[str, Any]] = {
    "Free": {
        "price": "$0",
        "usage": "Lead generation included",
        "limit": "50 leads/month or 10 leads/day",
        "features": [
            "Company URL",
            "Country",
            "Verified email / phone when available",
            "Basic industry",
            "Basic service reason",
            "CSV export",
            "No outreach automation",
        ],
    },
    "Pro": {
        "price": "$15/month",
        "usage": "500+ leads/month + outreach automation",
        "limit": "500+ leads/month",
        "features": [
            "Outreach email sending",
            "Email CRM",
            "Followups",
            "Reply checking",
            "CSV export",
            "Gmail automation",
            "Better automation workflow",
        ],
    },
    "Agency": {
        "price": "$20/month",
        "usage": "1000+ leads/month + agency workflow",
        "limit": "1000+ leads/month",
        "features": [
            "Everything in Pro",
            "Higher usage limits",
            "Agency/client workflow ready",
            "Priority future features",
        ],
    },
}


def normalize_plan_label(plan: str) -> str:
    value = str(plan or "").strip().title()
    if value in {"Pro", "Agency"}:
        return value
    return "Free"


def current_plan() -> str:
    auth = st.session_state.get("auth", {}) or {}
    return normalize_plan_label(str(auth.get("subscription_plan", "")))


def render_landing_styles() -> None:
    st.markdown(
        """
        <style>
        :root {
            --page-max-width: 1160px;
            --surface: rgba(15, 23, 42, .82);
            --surface-strong: rgba(2, 6, 23, .9);
            --surface-soft: rgba(15, 23, 42, .62);
            --line: rgba(148, 163, 184, .16);
            --line-strong: rgba(125, 211, 252, .2);
            --text: #e2e8f0;
            --text-strong: #f8fafc;
            --muted: #94a3b8;
            --accent: #22d3ee;
            --accent-2: #8b5cf6;
        }
        .stApp {
            color: var(--text);
            background:
                linear-gradient(180deg, rgba(14, 165, 233, .08), transparent 260px),
                linear-gradient(135deg, #060814 0%, #0b1020 54%, #080b13 100%);
        }
        .block-container {
            max-width: var(--page-max-width);
            padding-top: 2.2rem;
            padding-bottom: 2.5rem;
            padding-left: 1.6rem;
            padding-right: 1.6rem;
        }
        [data-testid="stHeader"] {
            background: transparent;
        }
        [data-testid="stToolbar"] {visibility: hidden; height: 0;}
        [data-testid="stDecoration"] {display: none;}
        [data-testid="stSidebar"] {
            background: #070b15;
            border-right: 1px solid rgba(148, 163, 184, .12);
        }
        [data-testid="stSidebar"] [data-testid="stSidebarContent"] {
            padding: 1.1rem .9rem;
        }
        [data-testid="stSidebar"] .stRadio [role="radiogroup"] {
            gap: .35rem;
        }
        [data-testid="stSidebar"] .stRadio [role="radio"] {
            border-radius: 8px;
            padding: .48rem .62rem;
            background: transparent;
            border: 1px solid transparent;
        }
        [data-testid="stSidebar"] .stRadio [role="radio"][aria-checked="true"] {
            background: rgba(15, 23, 42, .95);
            border-color: rgba(148, 163, 184, .18);
        }
        [data-testid="stSidebar"] .stButton > button,
        [data-testid="stSidebar"] .stCheckbox,
        [data-testid="stSidebar"] .stRadio {
            margin-bottom: .35rem;
        }
        [data-testid="stSidebar"] .stSlider {
            padding-bottom: .25rem;
        }
        [data-testid="stSidebar"] label {
            color: #cbd5e1 !important;
        }
        .app-shell {
            max-width: var(--page-max-width);
            margin: 0 auto;
        }
        .page-section {
            margin-top: .9rem;
        }
        .page-card {
            border: 1px solid var(--line);
            border-radius: 8px;
            background: var(--surface);
            box-shadow: 0 14px 34px rgba(0, 0, 0, .14), inset 0 1px 0 rgba(255, 255, 255, .035);
        }
        .page-card-inner {
            padding: 1rem;
        }
        .page-title {
            margin: 0;
            font-size: 1.45rem;
            line-height: 1.2;
            color: var(--text-strong);
            font-weight: 850;
            letter-spacing: 0;
        }
        .page-subtitle {
            margin: .35rem 0 0;
            color: var(--muted);
            line-height: 1.6;
        }
        .top-hero {
            display: flex;
            align-items: flex-start;
            justify-content: space-between;
            gap: 1rem;
            min-height: 156px;
            max-height: 220px;
            margin-top: .1rem;
            margin-bottom: .9rem;
            border: 1px solid var(--line);
            border-radius: 8px;
            background: rgba(15, 23, 42, .78);
            padding: 1.05rem;
            box-shadow: 0 16px 38px rgba(0, 0, 0, .16);
        }
        .hero-panel {
            border: 0;
            border-radius: 0;
            background: transparent;
            padding: 0;
            box-shadow: none;
        }
        .hero-stack {display: flex; flex-direction: column; gap: .45rem; min-width: 0;}
        .hero-title {
            margin: 0;
            color: var(--text-strong);
            font-size: 2rem;
            line-height: 1.15;
            font-weight: 800;
            letter-spacing: 0;
        }
        .hero-note {
            color: var(--muted);
            line-height: 1.5;
            margin: 0;
            max-width: 720px;
        }
        .hero-badge-row {
            display: flex;
            align-items: center;
            gap: .5rem;
            flex-wrap: wrap;
            margin-top: 0;
        }
        .plan-badge {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            padding: .32rem .72rem;
            border-radius: 999px;
            border: 1px solid rgba(34, 211, 238, .28);
            color: #c7f9ff;
            background: rgba(8, 47, 73, .42);
            font-size: .84rem;
            font-weight: 800;
            letter-spacing: 0;
        }
        .hero-metrics {
            display: grid;
            grid-template-columns: repeat(4, minmax(210px, 1fr));
            gap: .75rem;
            margin-top: .9rem;
        }
        .hero-metric {
            border: 1px solid var(--line);
            border-radius: 8px;
            background: rgba(15, 23, 42, .82);
            padding: .85rem;
            min-width: 0;
            word-break: normal;
            overflow-wrap: normal;
        }
        .hero-metric .label {color: var(--muted); font-size: .8rem; margin: 0 0 .35rem;}
        .hero-metric .value {color: var(--text-strong); font-size: 1.12rem; font-weight: 800; margin: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;}
        .hero-metric .hint {color: #cbd5e1; font-size: .82rem; margin: .2rem 0 0;}
        .landing-shell {
            position: relative;
            overflow: hidden;
            border: 1px solid var(--line);
            border-radius: 18px;
            padding: 1.35rem;
            background: linear-gradient(145deg, rgba(15, 23, 42, .82), rgba(2, 6, 23, .7));
            box-shadow: 0 24px 80px rgba(0, 0, 0, .34), inset 0 1px 0 rgba(255, 255, 255, .06);
        }
        .landing-shell:before {
            content: "";
            position: absolute;
            inset: -2px;
            pointer-events: none;
            background: linear-gradient(110deg, rgba(34, 211, 238, .16), transparent 34%, rgba(168, 85, 247, .14));
            filter: blur(28px);
        }
        .hero-kicker {
            display: inline-flex;
            align-items: center;
            gap: .45rem;
            border: 1px solid rgba(34, 211, 238, .32);
            border-radius: 999px;
            padding: .42rem .75rem;
            color: #bae6fd;
            background: rgba(8, 47, 73, .32);
            font-size: .86rem;
            font-weight: 700;
            letter-spacing: .01em;
        }
        .landing-title {
            font-size: clamp(1.05rem, 2vw, 1.35rem);
            color: #93c5fd;
            font-weight: 800;
            margin: 1.05rem 0 .3rem;
        }
        .hero-headline {
            font-size: clamp(2.45rem, 5vw, 5rem);
            line-height: .96;
            font-weight: 900;
            letter-spacing: 0;
            margin: .1rem 0 1rem;
            background: linear-gradient(92deg, #f8fafc 0%, #67e8f9 42%, #c084fc 78%, #f0abfc 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        .landing-tagline {
            font-size: 1.05rem;
            line-height: 1.75;
            color: #cbd5e1;
            max-width: 760px;
        }
        .cta-row {display: flex; gap: .75rem; flex-wrap: wrap; margin: 1.2rem 0 1.1rem;}
        .cta-pill {
            display: inline-flex;
            border-radius: 999px;
            padding: .76rem 1.05rem;
            font-weight: 800;
            border: 1px solid rgba(148, 163, 184, .22);
            color: #e2e8f0;
            background: rgba(15, 23, 42, .58);
        }
        .cta-pill.primary {
            color: #06101f;
            border-color: transparent;
            background: linear-gradient(90deg, #22d3ee, #a78bfa);
            box-shadow: 0 12px 34px rgba(34, 211, 238, .22);
        }
        .pricing-strip {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: .65rem;
            margin: 1.15rem 0 1.2rem;
        }
        .price-chip {
            border: 1px solid rgba(148, 163, 184, .18);
            border-radius: 12px;
            padding: .78rem;
            color: #dbeafe;
            background: rgba(15, 23, 42, .45);
        }
        .price-chip strong {display: block; color: #f8fafc; margin-bottom: .15rem;}
        .feature-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
            gap: .85rem;
            margin-top: 1.05rem;
        }
        .feature-card {
            border: 1px solid rgba(148, 163, 184, .18);
            border-radius: 8px;
            padding: 1rem;
            background: linear-gradient(150deg, rgba(15, 23, 42, .72), rgba(15, 23, 42, .36));
            box-shadow: inset 0 1px 0 rgba(255, 255, 255, .07), 0 16px 40px rgba(0, 0, 0, .18);
        }
        .feature-card h3 {margin: 0 0 .45rem 0; font-size: 1rem; color: #f8fafc;}
        .feature-card p {margin: 0; color: #cbd5e1; line-height: 1.55;}
        .auth-card {
            border: 1px solid rgba(125, 211, 252, .24);
            border-radius: 8px;
            padding: 1.15rem 1.15rem .9rem;
            background: linear-gradient(180deg, rgba(15, 23, 42, .86), rgba(2, 6, 23, .74));
            box-shadow: 0 22px 70px rgba(0, 0, 0, .35), 0 0 0 1px rgba(255, 255, 255, .04) inset;
        }
        .auth-card h2 {margin: 0 0 .35rem; color: #f8fafc; font-size: 1.35rem;}
        .auth-card p {color: #cbd5e1; margin-top: 0;}
        .auth-card [data-testid="stTextInput"] label {color: #dbeafe;}
        .auth-card .stTabs [data-baseweb="tab-list"] {gap: .35rem;}
        .auth-card .stTabs [data-baseweb="tab"] {
            border-radius: 999px;
            color: #dbeafe;
            background: rgba(15, 23, 42, .68);
            border: 1px solid rgba(148, 163, 184, .16);
            padding-left: .85rem;
            padding-right: .85rem;
        }
        .auth-card input {
            color: #f8fafc !important;
            background: rgba(2, 6, 23, .5) !important;
            border-color: rgba(125, 211, 252, .22) !important;
        }
        .auth-card + div, .auth-card ~ div {color: #e2e8f0;}
        .auth-card .stButton > button, .auth-card [data-testid="stFormSubmitButton"] button {
            border: 0;
            color: #06101f;
            font-weight: 900;
            background: linear-gradient(90deg, #22d3ee, #a78bfa);
            box-shadow: 0 12px 32px rgba(34, 211, 238, .18);
        }
        .plan-card {border: 1px solid rgba(148, 163, 184, .22); border-radius: 8px; padding: 1rem; background: rgba(15, 23, 42, .72); color: #e2e8f0;}
        .plan-card-active {border: 2px solid #22d3ee;}
        .plan-price {font-size: 1.4rem; font-weight: 800; color: #f8fafc;}
        .premium-badge {
            display: inline-flex;
            align-items: center;
            gap: .35rem;
            border-radius: 999px;
            padding: .26rem .7rem;
            border: 1px solid rgba(244, 114, 182, .24);
            background: rgba(76, 29, 149, .26);
            color: #f5d0fe;
            font-size: .78rem;
            font-weight: 800;
        }
        .section-title {
            margin: 0 0 .25rem;
            color: var(--text-strong);
            font-size: 1.15rem;
            font-weight: 850;
        }
        .section-caption {
            margin: 0 0 .9rem;
            color: var(--muted);
            line-height: 1.55;
        }
        .section-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
            gap: .85rem;
        }
        .panel-card {
            border: 1px solid var(--line);
            border-radius: 8px;
            background: var(--surface);
            padding: .9rem;
            min-width: 0;
            word-break: normal;
            overflow-wrap: normal;
        }
        .panel-card h3, .panel-card h4 {color: var(--text-strong); margin-top: 0;}
        .panel-card p, .panel-card li, .panel-card caption {color: var(--text);}
        .locked-grid {
            display: grid;
            grid-template-columns: repeat(3, minmax(220px, 1fr));
            gap: .75rem;
            margin-top: .8rem;
        }
        .dashboard-input [data-testid="stTextInput"] {
            margin-bottom: .65rem;
        }
        .dashboard-input input {
            background: rgba(2, 6, 23, .48) !important;
            border-color: rgba(148, 163, 184, .22) !important;
            color: var(--text-strong) !important;
        }
        .dashboard-input label {
            color: #dbeafe !important;
            font-weight: 650;
        }
        .dashboard-actions .stButton > button,
        .dashboard-actions [data-testid="stFormSubmitButton"] button,
        .dashboard-actions .stDownloadButton button {
            width: 100%;
            min-height: 2.55rem;
            border-radius: 8px;
        }
        .dashboard-actions .stButton > button {
            background: linear-gradient(90deg, #22d3ee, #a78bfa);
            color: #06101f;
            font-weight: 900;
            border: 0;
        }
        .dashboard-actions .premium-action {
            border: 1px solid rgba(244, 114, 182, .22);
            background: rgba(76, 29, 149, .18);
            color: #f5d0fe;
        }
        .locked-action {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: .5rem;
        }
        .settings-note {
            color: var(--muted);
            line-height: 1.55;
            margin: .2rem 0 .85rem;
        }
        .summary-stack {
            display: flex;
            flex-direction: column;
            align-items: flex-end;
            gap: .4rem;
            color: var(--muted);
            font-size: .86rem;
            max-width: 380px;
            text-align: right;
        }
        .summary-stack span {
            display: block;
            max-width: 100%;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .admin-title {
            margin: 0 0 .8rem;
            color: var(--text-strong);
            font-size: 1.45rem;
            line-height: 1.2;
            font-weight: 800;
            letter-spacing: 0;
        }
        .main-title {
            margin: 0 0 .3rem;
            color: var(--text-strong);
            font-size: 1.4rem;
            line-height: 1.2;
            font-weight: 800;
            letter-spacing: 0;
        }
        .main-subtitle {
            color: var(--muted);
            margin: 0 0 .85rem;
            line-height: 1.5;
        }
        @media (max-width: 980px) {
            .block-container {padding-left: 1rem; padding-right: 1rem; padding-top: 1.4rem;}
            .top-hero {
                display: block;
                max-height: none;
                min-height: 0;
            }
            .summary-stack {
                align-items: flex-start;
                text-align: left;
                margin-top: .85rem;
            }
            .hero-metrics,
            .locked-grid {
                grid-template-columns: 1fr;
            }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def api_headers() -> Dict[str, str]:
    token = str(st.session_state.get("auth", {}).get("token", "")).strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


def api_request(method: str, path: str, **kwargs: Any) -> httpx.Response:
    timeout = kwargs.pop("timeout", 60)
    headers = dict(kwargs.pop("headers", {}) or {})
    headers.update(api_headers())
    with httpx.Client(timeout=timeout) as client:
        return client.request(method, f"{API_BASE_URL}{path}", headers=headers, **kwargs)


def get_auth_role() -> str:
    token = str(st.session_state.get("auth", {}).get("token", "")).strip()
    if not token:
        return ""
    try:
        payload = decode_jwt_token(token)
    except Exception:
        return ""
    return str(payload.get("role", "")).strip().lower()


def is_admin_user() -> bool:
    return get_auth_role() == "admin"


def require_login() -> TenantContext | None:
    auth = st.session_state.get("auth", {})
    if auth.get("tenant_id"):
        return TenantContext(
            tenant_id=str(auth["tenant_id"]),
            user_id=str(auth.get("user_id", "")),
            metadata={"email": str(auth.get("email", ""))},
        )

    render_landing_styles()
    st.markdown('<div class="landing-shell">', unsafe_allow_html=True)
    hero_col, auth_col = st.columns([1.35, .9], gap="large")
    with hero_col:
        st.markdown('<div class="hero-kicker">Free Lead Finder Beta — Outreach automation available in Pro</div>', unsafe_allow_html=True)
        st.markdown(f'<div class="landing-title">{PRODUCT_NAME}</div>', unsafe_allow_html=True)
        st.markdown('<div class="hero-headline">Find B2B Leads Before Your Competitors Do</div>', unsafe_allow_html=True)
        st.markdown(
            '<div class="landing-tagline">Search by niche and country. Generate company URLs, public emails, phones, and export-ready CSVs in seconds.</div>',
            unsafe_allow_html=True,
        )
        st.markdown(
            """
            <div class="cta-row">
                <span class="cta-pill primary">Create Free Account</span>
                <span class="cta-pill">Login</span>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.markdown(
            """
            <div class="pricing-strip">
                <div class="price-chip"><strong>Free</strong>Lead Finder</div>
                <div class="price-chip"><strong>Pro</strong>$15/month — Outreach + Email CRM</div>
                <div class="price-chip"><strong>Agency</strong>$20/month — 1000+ leads + agency workflow</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.markdown(
            """
            <div class="feature-grid">
                <div class="feature-card"><h3>Smart Lead Discovery</h3><p>Find potential companies from public web sources by niche and country.</p></div>
                <div class="feature-card"><h3>Contact Extraction</h3><p>Extract company URLs, business emails, and phone numbers when available.</p></div>
                <div class="feature-card"><h3>CSV Export Ready</h3><p>Download clean leads and start manual outreach instantly.</p></div>
                <div class="feature-card"><h3>Pro Automation</h3><p>Upgrade later for Gmail outreach, followups, reply tracking, and CRM workflow.</p></div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with auth_col:
        st.markdown('<div class="auth-card"><h2>Access your lead cockpit</h2><p>Create a Free workspace or log into an existing tenant.</p>', unsafe_allow_html=True)
        login_tab, signup_tab = st.tabs(["Login", "Create Free Account"])
        with login_tab:
            with st.form("admin_login"):
                tenant_id = st.text_input("Tenant ID", value="", placeholder="Tenant ID")
                email = st.text_input("Email", value="", placeholder="you@company.com")
                password = st.text_input("Password", value="", type="password", placeholder="Enter password")
                submitted = st.form_submit_button("Login", use_container_width=True)
            if submitted:
                try:
                    response = api_request(
                        "POST",
                        "/login",
                        json={
                            "tenant_id": tenant_id.strip(),
                            "email": email.strip(),
                            "password": password,
                        },
                    )
                    payload = response.json()
                    if not response.is_success:
                        st.error(str(payload.get("detail", "Login failed.")))
                        return None
                    st.session_state["auth"] = payload
                    st.session_state["latest_subscription"] = {}
                    st.session_state["plan_onboarding_seen"] = ""
                    st.rerun()
                except Exception as error:
                    st.error(str(error))
        with signup_tab:
            with st.form("signup_form"):
                signup_tenant = st.text_input("Workspace / Tenant ID", value="", placeholder="my-business")
                signup_name = st.text_input("Business Name", value="", placeholder="Acme Agency")
                signup_email = st.text_input("Work Email", value="", placeholder="you@company.com")
                signup_full_name = st.text_input("Your Name", value="", placeholder="Your name")
                signup_password = st.text_input("Password", value="", type="password", placeholder="Create a password")
                signup_submitted = st.form_submit_button("Start Free", use_container_width=True)
            if signup_submitted:
                tenant_id = signup_tenant.strip()
                tenant_name = signup_name.strip() or tenant_id
                if not tenant_id or not signup_email.strip() or not signup_password.strip():
                    st.error("Tenant ID, email, and password are required.")
                    return None
                try:
                    response = api_request(
                        "POST",
                        "/signup",
                        json={
                            "tenant_id": tenant_id,
                            "tenant_name": tenant_name,
                            "tenant_slug": tenant_id.lower().replace(" ", "-"),
                            "email": signup_email.strip(),
                            "password": signup_password.strip(),
                            "full_name": signup_full_name.strip() or signup_email.strip(),
                            "plan": "Free",
                        },
                    )
                    payload = response.json()
                    if not response.is_success:
                        st.error(str(payload.get("detail", "Signup failed.")))
                        return None
                    st.session_state["auth"] = payload
                    st.session_state["latest_subscription"] = {}
                    st.session_state["plan_onboarding_seen"] = ""
                    st.rerun()
                except Exception as error:
                    st.error(str(error))
        st.markdown('</div>', unsafe_allow_html=True)
    st.markdown('</div>', unsafe_allow_html=True)
    return None


def infer_country(location: str) -> str:
    value = str(location or "").strip()
    if not value:
        return "Unknown"
    parts = [part.strip() for part in value.split(",") if part.strip()]
    return parts[-1][:60] if parts else value[:60]


def classify_pipeline_stage(row: pd.Series) -> str:
    reply_status = str(row.get("ReplyStatus", "")).strip().lower()
    reply_classification = str(row.get("ReplyClassification", "")).strip()
    email_status = str(row.get("EmailStatus", "")).strip().lower()
    followup_count = int(row.get("FollowupCount", 0) or 0)
    if reply_status in {"replied_positive", "interested"} or reply_classification == "Interested":
        return "Interested"
    if reply_status in {"replied_negative", "not_interested"}:
        return "Replied"
    if reply_status != "no_reply":
        return "Replied"
    if email_status == "sent" and followup_count > 0:
        return "Follow-up Active"
    if email_status == "sent":
        return "Outreach Sent"
    return "New Lead"


STANDARD_LEAD_EXPORT_COLUMNS = [
    "company_url",
    "country",
    "verified_email",
    "service_reason",
    "industry",
    "score",
    "outreach_status",
    "followup_count",
    "reply_status",
    "last_reply_at",
]


def load_dashboard_data(tenant: TenantContext) -> pd.DataFrame:
    response = api_request("GET", "/leads")
    payload = response.json()
    if not response.is_success:
        raise RuntimeError(str(payload.get("detail", "Could not load leads.")))
    items = payload.get("items", [])
    rows = []
    for item in items:
        rows.append(
            {
                "company_url": str(item.get("company_url", "") or "").strip(),
                "country": str(item.get("country", "") or "").strip(),
                "verified_email": str(item.get("verified_email", "") or "").strip().lower(),
                "service_reason": str(item.get("service_reason", "") or "").strip(),
                "industry": str(item.get("industry", "") or "").strip(),
                "score": item.get("score", 0),
                "outreach_status": str(item.get("outreach_status", "") or "").strip().lower(),
                "followup_count": item.get("followup_count", 0),
                "reply_status": str(item.get("reply_status", "") or "").strip().lower(),
                "last_reply_at": str(item.get("last_reply_at", "") or "").strip(),
            }
        )
    if not rows:
        return pd.DataFrame(columns=STANDARD_LEAD_EXPORT_COLUMNS)
    frame = pd.DataFrame(rows)
    frame["score"] = pd.to_numeric(frame.get("score", pd.Series(0, index=frame.index)), errors="coerce").fillna(0)
    frame["followup_count"] = pd.to_numeric(frame.get("followup_count", pd.Series(0, index=frame.index)), errors="coerce").fillna(0).astype(int)
    for column in STANDARD_LEAD_EXPORT_COLUMNS:
        if column not in frame.columns:
            frame[column] = ""
    return frame[STANDARD_LEAD_EXPORT_COLUMNS]


def filtered_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    st.sidebar.header("Filters")
    score_range = st.sidebar.slider("Lead Score", int(frame["score"].min()), max(int(frame["score"].max()), 10), (int(frame["score"].min()), max(int(frame["score"].max()), 10)))
    statuses = sorted(frame["outreach_status"].dropna().astype(str).unique())
    status = st.sidebar.multiselect("Outreach Status", statuses, default=list(statuses))
    countries = st.sidebar.multiselect("Country", sorted(frame["country"].dropna().astype(str).unique()))
    industries = st.sidebar.multiselect("Industry", sorted(frame["industry"].dropna().astype(str).unique()))
    reply_types = st.sidebar.multiselect("Reply Type", sorted(frame["reply_status"].dropna().astype(str).unique()))
    filtered = frame[frame["score"].between(score_range[0], score_range[1])]
    if status:
        filtered = filtered[filtered["outreach_status"].isin(status)]
    if countries:
        filtered = filtered[filtered["country"].isin(countries)]
    if industries:
        filtered = filtered[filtered["industry"].isin(industries)]
    if reply_types:
        filtered = filtered[filtered["reply_status"].isin(reply_types)]
    return filtered


def parse_api_json(response: httpx.Response, fallback_reason: str = "INVALID_API_RESPONSE") -> Dict[str, Any]:
    raw_text = (response.text or "").strip()
    if not raw_text:
        st.session_state["last_api_error"] = {
            "status_code": response.status_code,
            "reason": fallback_reason,
            "body_preview": "",
            "headers": dict(response.headers),
        }
        st.error("Backend returned an empty response.")
        return {"status": "FAILED", "reason": fallback_reason}

    try:
        return response.json()
    except Exception as error:
        st.session_state["last_api_error"] = {
            "status_code": response.status_code,
            "reason": fallback_reason,
            "body_preview": raw_text[:500],
            "headers": dict(response.headers),
            "error": str(error),
        }
        st.error("Backend returned invalid JSON. Check logs for details.")
        return {"status": "FAILED", "reason": fallback_reason}


def enqueue_job(agent_name: str, payload: Dict[str, Any] | None = None, *, run_now: bool = True) -> None:
    response = api_request("POST", "/jobs", json={"agent_name": agent_name, "payload": payload or {}})
    body = parse_api_json(response)
    if not response.is_success:
        raise RuntimeError(str(body.get("detail", f"Could not enqueue {agent_name}.")))
    if run_now:
        try:
            run_response = api_request("POST", "/jobs/run-once", json={"job_type": agent_name})
        except Exception as error:
            st.session_state["last_api_error"] = {
                "status_code": None,
                "reason": "INVALID_API_RESPONSE",
                "body_preview": "",
                "error": str(error),
            }
            st.error(f"Could not trigger job execution: {error}")
            return
        run_body = parse_api_json(run_response)
        if not run_response.is_success:
            st.error(str(run_body.get("detail", f"Could not run {agent_name}.")))
            return
        if str(run_body.get("status", "")).strip().lower() == "failed":
            st.error(str(run_body.get("message", f"Could not run {agent_name}.")))
            return


def render_actions(tenant: TenantContext) -> None:
    pro_enabled = has_pro_features()
    st.markdown(
        """
        <div class="page-section page-card">
            <div class="page-card-inner dashboard-actions">
                <div class="section-title">Lead Generation Settings</div>
                <div class="section-caption">Pick a niche and region, then launch lead discovery or automation tasks.</div>
        """,
        unsafe_allow_html=True,
    )
    col_ind, col_loc = st.columns(2)
    with col_ind:
        target_industry = st.text_input("Target Industry", placeholder="e.g. software, logistics")
    with col_loc:
        target_country = st.text_input("Target Country", placeholder="e.g. Saudi Arabia, USA")

    busy = bool(st.session_state.get("lead_generation_busy", False))
    if st.button("Generate Leads", use_container_width=True, disabled=busy):
        query_parts = []
        if target_industry.strip():
            query_parts.append(f"{target_industry.strip()} companies")
        if target_country.strip():
            query_parts.append(f"in {target_country.strip()}")
        search_query = " ".join(query_parts)

        st.session_state["lead_generation_busy"] = True
        with st.spinner("AI agent is finding leads... this may take 1-3 minutes."):
            st.info("AI agent is finding leads... this may take 1-3 minutes.")
            for step in ["Searching businesses...", "Scanning websites...", "Extracting contacts...", "Saving leads..."]:
                st.write(step)
            try:
                enqueue_job("lead_generation", {"limit": 10, "query": search_query}, run_now=True)
                st.success("Lead generation completed. Refresh to see the latest saved leads.")
                st.caption("Job ran successfully.")
            finally:
                st.session_state["lead_generation_busy"] = False

    action_cols = st.columns(3)
    with action_cols[0]:
        if st.button("Send Outreach - Pro", use_container_width=True, disabled=not pro_enabled):
            with st.spinner("Queueing outreach..."):
                enqueue_job("outreach")
                st.success("Outreach job queued.")
    with action_cols[1]:
        if st.button("Check Replies - Pro", use_container_width=True, disabled=not pro_enabled):
            with st.spinner("Queueing reply check..."):
                enqueue_job("reply_monitor", {"mode": "once"})
                st.success("Reply monitor job queued.")
    with action_cols[2]:
        if st.button("Run Followups - Pro", use_container_width=True, disabled=not pro_enabled):
            with st.spinner("Queueing followups..."):
                enqueue_job("followup")
                st.success("Follow-up job queued.")

    if not pro_enabled:
        locked_features = ["Gmail Automation", "Email CRM", "AI Smart Scoring"]
        locked_cards = "".join(
            (
                '<div class="panel-card">'
                '<span class="premium-badge">Pro</span>'
                f'<h4>{html.escape(label)}</h4>'
                '<p>Upgrade to unlock automated outreach, reply tracking, and Gmail workflows.</p>'
                "</div>"
            )
            for label in locked_features
        )
        st.markdown(f'<div class="locked-grid">{locked_cards}</div>', unsafe_allow_html=True)
    st.markdown("</div></div>", unsafe_allow_html=True)


def load_recent_jobs() -> List[Dict[str, Any]]:
    try:
        response = api_request("GET", "/jobs/recent")
        payload = parse_api_json(response)
        if response.is_success:
            return list(payload.get("items", []) or [])
    except Exception:
        return []
    return []


def render_generation_status_card(snapshot: Dict[str, Any]) -> None:
    jobs = load_recent_jobs()
    latest_job = jobs[0] if jobs else {}
    queued_or_running = [
        job for job in jobs if str(job.get("status", "")).strip().lower() in {"queued", "running"}
    ]
    metrics = [
        ("Last job status", str(latest_job.get("status", "unknown") or "unknown").title(), "Current pipeline"),
        ("Total leads", str(int(snapshot.get("lead_count", 0) or 0)), "Saved in workspace"),
        ("Jobs queued/running", str(len(queued_or_running)), "Active processing"),
        ("Last refresh", datetime.utcnow().strftime("%H:%M:%S UTC"), "Latest sync"),
    ]
    cards = "".join(
        (
            '<div class="hero-metric">'
            f'<p class="label">{html.escape(label)}</p>'
            f'<p class="value">{html.escape(value)}</p>'
            f'<p class="hint">{html.escape(hint)}</p>'
            "</div>"
        )
        for label, value, hint in metrics
    )
    st.markdown(f'<div class="page-section"><div class="hero-metrics">{cards}</div></div>', unsafe_allow_html=True)


def render_dashboard_page(frame: pd.DataFrame, snapshot: Dict[str, Any]) -> None:
    st.markdown('<div class="page-section page-card"><div class="page-card-inner">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Pipeline Overview</div>', unsafe_allow_html=True)
    st.markdown('<div class="section-caption">A compact view of output, sends, replies, and current workload.</div>', unsafe_allow_html=True)
    metrics = st.columns(4)
    metrics[0].metric("Leads", int(snapshot.get("lead_count", 0)))
    metrics[1].metric("Sent", int(snapshot.get("sent_count", 0)))
    metrics[2].metric("Replies", int(snapshot.get("reply_count", 0)))
    metrics[3].metric("Jobs", int(snapshot.get("job_count", 0)))
    if frame.empty:
        st.info("No leads yet. Enter a niche and country to generate your first leads.")
        st.markdown("</div></div>", unsafe_allow_html=True)
        return
    chart_left, chart_right = st.columns(2)
    with chart_left:
        st.plotly_chart(px.histogram(frame, x="score", nbins=10, title="Lead Scores"), use_container_width=True)
    with chart_right:
        st.plotly_chart(px.pie(frame, names="outreach_status", title="Pipeline"), use_container_width=True)
    st.markdown("</div></div>", unsafe_allow_html=True)


def render_table_page(frame: pd.DataFrame, columns: List[str]) -> None:
    visible = frame.copy()
    for column in columns:
        if column not in visible.columns:
            visible[column] = ""
    export_frame = visible[columns]
    if export_frame.empty:
        st.info("No leads yet. Enter a niche and country to generate your first leads.")
    else:
        st.caption("Showing latest leads only.")
    st.dataframe(export_frame, use_container_width=True, hide_index=True)
    csv_frame = sanitize_csv_frame(export_frame)
    st.download_button(
        "Download CSV",
        csv_frame.to_csv(index=False).encode("utf-8"),
        file_name="leads.csv",
        mime="text/csv",
        use_container_width=True,
    )


def sanitize_csv_frame(frame: pd.DataFrame) -> pd.DataFrame:
    protected = frame.copy()
    dangerous_prefixes = ("=", "+", "-", "@")
    for column in protected.columns:
        protected[column] = protected[column].map(
            lambda value: f"'{value}" if isinstance(value, str) and value.startswith(dangerous_prefixes) else value
        )
    return protected


def load_uploaded_json(uploaded_file: Any) -> Dict[str, Any]:
    if uploaded_file is None:
        return {}
    raw = uploaded_file.getvalue()
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(str(raw or "{}"))


def extract_google_oauth_client(credentials_json: Dict[str, Any]) -> Dict[str, str]:
    client_config = credentials_json.get("installed") or credentials_json.get("web") or credentials_json
    return {
        "client_id": str(client_config.get("client_id", "")).strip(),
        "client_secret": str(client_config.get("client_secret", "")).strip(),
    }


def render_settings_page() -> None:
    st.markdown(
        """
        <div class="page-section">
            <div class="main-title">Settings</div>
            <div class="main-subtitle">Manage workspace integrations and automation setup.</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    render_gmail_settings()


def render_gmail_settings() -> None:
    st.markdown(
        """
        <div class="page-section dashboard-actions">
            <div class="page-card">
                <div class="page-card-inner">
                    <div class="section-title">Gmail Automation Setup</div>
                    <div class="settings-note">Beta setup requires Google credentials.json and token.json. One-click Gmail connect is coming soon.</div>
        """,
        unsafe_allow_html=True,
    )
    if not has_pro_features():
        st.warning(LOCKED_FEATURE_MESSAGE)
        st.markdown("</div></div></div>", unsafe_allow_html=True)
        return
    try:
        status_response = api_request("GET", "/settings/providers/gmail/status")
        status = parse_api_json(status_response)
    except Exception as error:
        st.error(f"Could not load Gmail status: {error}")
        status = {"configured": False, "sender_email": ""}
    configured = bool(status.get("configured"))
    sender_email = str(status.get("sender_email", "") or "")
    st.caption(f"Status: {'connected' if configured else 'not connected'}")
    if sender_email:
        st.caption(f"Sender: {sender_email}")

    with st.form("gmail_credentials_form"):
        credentials_file = st.file_uploader("Upload credentials.json", type=["json"])
        token_file = st.file_uploader("Upload token.json", type=["json"])
        sender_email_input = st.text_input("Sender email", value=sender_email, placeholder="sales@company.com")
        submitted = st.form_submit_button("Save Gmail Connection", use_container_width=True)
    if submitted:
        if credentials_file is None or token_file is None or not sender_email_input.strip():
            st.error("credentials.json, token.json, and sender email are required.")
            st.markdown("</div></div></div>", unsafe_allow_html=True)
            return
        try:
            credentials_json = load_uploaded_json(credentials_file)
            token_json = load_uploaded_json(token_file)
            client = extract_google_oauth_client(credentials_json)
            refresh_token = str(token_json.get("refresh_token", "")).strip()
        except Exception as error:
            st.error(f"Could not read Gmail setup files: {error}")
            st.markdown("</div></div></div>", unsafe_allow_html=True)
            return
        if not client["client_id"] or not client["client_secret"] or not refresh_token:
            st.error("Uploaded files are missing client_id, client_secret, or refresh_token.")
            st.markdown("</div></div></div>", unsafe_allow_html=True)
            return
        payload = {
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "refresh_token": refresh_token,
            "sender_email": sender_email_input.strip(),
        }
        response = api_request("POST", "/settings/providers/gmail", json=payload)
        body = parse_api_json(response)
        if response.is_success:
            st.success("Gmail connection saved.")
            st.rerun()
        else:
            st.error(str(body.get("detail", "Could not save Gmail connection.")))
    st.markdown("</div></div></div>", unsafe_allow_html=True)


def infer_subscription_status() -> str:
    latest = st.session_state.get("latest_subscription", {}) or {}
    latest_status = str(latest.get("status", "")).strip()
    if latest_status:
        return latest_status
    auth = st.session_state.get("auth", {})
    plan = str(auth.get("subscription_plan", "")).strip()
    return "active" if plan else "inactive"


def has_pro_features() -> bool:
    return current_plan() in {"Pro", "Agency"}


def render_plan_card(plan: str, active: bool = False) -> None:
    details = PLAN_DETAILS[plan]
    classes = "plan-card plan-card-active" if active else "plan-card"
    items = "".join(f"<li>{feature}</li>" for feature in details["features"])
    st.markdown(
        f"""
        <div class="{classes}">
            <h3>{plan}</h3>
            <div class="plan-price">{details["price"]}</div>
            <p><strong>{details["limit"]}</strong></p>
            <ul>{items}</ul>
        </div>
        """,
        unsafe_allow_html=True,
    )


def request_plan_upgrade(plan: str) -> None:
    if plan == "Free":
        st.session_state["auth"] = {**st.session_state.get("auth", {}), "subscription_plan": "Free"}
        st.success("Free plan selected. Lead generation and CSV export are ready.")
        return
    try:
        response = api_request("POST", "/billing/subscribe", json={"plan": plan})
        payload = response.json()
        if response.is_success:
            payload["status"] = "pending"
            st.session_state["latest_subscription"] = payload
            st.info("Upgrade request submitted. Admin will contact you.")
        else:
            st.warning("Contact admin to activate this plan.")
    except Exception:
        st.warning("Contact admin to activate this plan.")


def render_plan_onboarding() -> bool:
    auth = st.session_state.get("auth", {}) or {}
    tenant_id = str(auth.get("tenant_id", ""))
    if st.session_state.get("plan_onboarding_seen") == tenant_id:
        return False
    render_landing_styles()
    st.markdown("## Choose how you want to use Lead Hunter AI")
    st.caption("Free gets you started with lead generation. Upgrade when you are ready to automate outreach.")
    active_plan = current_plan()
    columns = st.columns(3)
    for column, plan in zip(columns, ["Free", "Pro", "Agency"]):
        with column:
            render_plan_card(plan, active=plan == active_plan)
            if plan == active_plan:
                st.success("Current plan")
            elif st.button(f"Select {plan}", key=f"onboarding_{plan}", use_container_width=True):
                request_plan_upgrade(plan)
    st.write("")
    if st.button("Continue to dashboard", use_container_width=True):
        st.session_state["plan_onboarding_seen"] = tenant_id
        st.rerun()
    return True


def render_dashboard_header() -> None:
    auth = st.session_state.get("auth", {}) or {}
    plan = current_plan()
    tenant_id = html.escape(str(auth.get("tenant_id", "")))
    user_email = html.escape(str(auth.get("email", "")))
    usage = html.escape(PLAN_DETAILS[plan]["usage"])
    status = html.escape(infer_subscription_status().replace("_", " ").title())
    st.markdown(
        f"""
        <div class="top-hero">
            <div class="hero-stack">
                <h1 class="hero-title">Lead Hunter AI</h1>
                <p class="hero-note">Generate leads, review contacts, export CSVs, and manage outreach from one clean workspace.</p>
            </div>
            <div class="summary-stack">
                <div class="hero-badge-row">
                    <span class="plan-badge">{html.escape(plan)}</span>
                    <span class="premium-badge">{status}</span>
                </div>
                <span>{tenant_id}</span>
                <span>{user_email}</span>
                <span>{usage}</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_subscription_card() -> None:
    auth = st.session_state.get("auth", {})
    st.markdown('<div class="page-section page-card"><div class="page-card-inner">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Subscription</div>', unsafe_allow_html=True)
    st.markdown('<div class="section-caption">Keep an eye on tenant plan and billing state.</div>', unsafe_allow_html=True)
    col1, col2, col3 = st.columns(3)
    col1.metric("Tenant", str(auth.get("tenant_id", "")))
    col2.metric("Plan", current_plan())
    col3.metric("Status", infer_subscription_status().replace("_", " ").title())
    st.markdown("</div></div>", unsafe_allow_html=True)


def render_latest_subscription_response() -> None:
    latest = st.session_state.get("latest_subscription", {}) or {}
    if not latest:
        return
    st.success("Subscription request created.")
    st.code(str(latest.get("payment_reference_id", "")), language=None)
    instructions = latest.get("payment_instructions", {}) or {}
    left, right = st.columns(2)
    with left:
        st.markdown("**Nayapay**")
        st.write(f"Name: {instructions.get('nayapay', {}).get('account_name', '')}")
        st.write(f"ID: {instructions.get('nayapay', {}).get('account_id', '')}")
        st.markdown("**Sadapay**")
        st.write(f"Name: {instructions.get('sadapay', {}).get('account_name', '')}")
        st.write(f"ID: {instructions.get('sadapay', {}).get('account_id', '')}")
    with right:
        qr_code_url = str(latest.get("qr_code_url", "")).strip()
        if qr_code_url:
            st.image(qr_code_url, caption="Payment QR", use_container_width=True)
        elif Path(DEFAULT_PAYMENT_QR_PATH).exists():
            st.image(DEFAULT_PAYMENT_QR_PATH, caption="Nayapay QR", use_container_width=True)
        else:
            st.info("QR code not available in API response.")
    next_steps = latest.get("next_steps", []) or []
    if next_steps:
        st.markdown("**Next steps**")
        for item in next_steps:
            st.write(f"- {item}")


def render_payment_history() -> None:
    st.subheader("Payment History")
    latest = st.session_state.get("latest_subscription", {}) or {}
    if not latest:
        st.info("No payment requests created in this session. Backend payment-history endpoint is not available yet.")
        return
    history_row = {
        "Reference": latest.get("payment_reference_id", ""),
        "Plan": latest.get("plan_name", ""),
        "Amount": latest.get("price", ""),
        "Status": latest.get("status", "pending"),
    }
    st.dataframe(pd.DataFrame([history_row]), use_container_width=True, hide_index=True)


def render_subscribe_section() -> None:
    st.subheader("Subscribe")
    default_plan = current_plan()
    with st.form("subscribe_form"):
        plan = st.selectbox("Choose Plan", ["Free", "Pro", "Agency"], index=max(["Free", "Pro", "Agency"].index(default_plan) if default_plan in {"Free", "Pro", "Agency"} else 0, 0))
        submitted = st.form_submit_button("Subscribe", use_container_width=True)
    if submitted:
        if plan == "Free":
            st.session_state["auth"] = {**st.session_state.get("auth", {}), "subscription_plan": "Free"}
            st.success("Free plan selected. Lead generation and CSV export are available.")
            return
        with st.spinner("Creating subscription request..."):
            try:
                response = api_request("POST", "/billing/subscribe", json={"plan": plan})
                payload = response.json()
                if response.is_success:
                    payload["status"] = "pending"
                    st.session_state["latest_subscription"] = payload
                    st.info("Upgrade request submitted. Admin will contact you.")
                else:
                    st.warning("Contact admin to activate this plan.")
            except Exception as error:
                st.error(str(error))
    render_latest_subscription_response()


def render_proof_upload() -> None:
    st.subheader("Upload Payment Proof")
    with st.form("proof_upload_form"):
        reference_id = st.text_input("Payment Reference ID")
        proof_file = st.file_uploader("Proof File", type=["png", "jpg", "jpeg", "pdf"])
        submitted = st.form_submit_button("Submit Proof", use_container_width=True)
    if submitted:
        if not reference_id.strip() or proof_file is None:
            st.error("Payment reference ID and proof file are required.")
            return
        mime_type = proof_file.type or mimetypes.guess_type(proof_file.name)[0] or "application/octet-stream"
        files = {"proof_file": (proof_file.name, proof_file.getvalue(), mime_type)}
        data = {"reference_id": reference_id.strip()}
        with st.spinner("Uploading proof..."):
            try:
                response = api_request("POST", "/billing/upload-proof", data=data, files=files)
                payload = response.json()
                if response.is_success:
                    st.session_state["latest_subscription"] = {
                        **(st.session_state.get("latest_subscription", {}) or {}),
                        "payment_reference_id": payload.get("payment_reference_id", reference_id.strip()),
                        "status": payload.get("status", "pending_verification"),
                        "proof_url": payload.get("proof_url", ""),
                    }
                    st.success("Proof submitted. Status: pending_verification")
                else:
                    st.error(str(payload.get("detail", "Proof upload failed.")))
            except Exception as error:
                st.error(str(error))


def render_billing_page() -> None:
    render_subscription_card()
    st.markdown('<div class="page-section page-card"><div class="page-card-inner">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Billing</div>', unsafe_allow_html=True)
    st.markdown('<div class="section-caption">Plan selection, subscription requests, and payment proof uploads.</div>', unsafe_allow_html=True)
    render_payment_history()
    render_subscribe_section()
    render_proof_upload()
    st.markdown("</div></div>", unsafe_allow_html=True)


def render_admin_payments() -> None:
    st.subheader("Payments")
    st.info("Admin payment list/reject endpoints are not available in the current backend. Approval remains available by reference ID.")
    with st.form("approve_payment_form"):
        reference_id = st.text_input("Payment Reference ID")
        submitted = st.form_submit_button("Approve Payment", use_container_width=True)
    if submitted:
        if not reference_id.strip():
            st.error("Payment reference ID is required.")
            return
        with st.spinner("Approving payment..."):
            try:
                response = api_request("POST", "/admin/payments/approve", json={"payment_reference_id": reference_id.strip()})
                payload = response.json()
                if response.is_success:
                    st.success(f"Payment approved: {payload.get('payment_reference_id', '')}")
                else:
                    st.error(str(payload.get("detail", "Payment approval failed.")))
            except Exception as error:
                st.error(str(error))
    st.caption("Reject action requires `/admin/payments/reject`, which is not present in the current backend.")


def admin_get(path: str) -> Dict[str, Any]:
    response = api_request("GET", path)
    payload = parse_api_json(response)
    if not response.is_success:
        st.error(str(payload.get("detail", "Admin request failed.")))
        return {}
    return payload


def render_admin_summary() -> None:
    st.markdown(
        '<div class="page-section"><div class="admin-title">Admin Dashboard <span class="plan-badge">Admin</span></div></div>',
        unsafe_allow_html=True,
    )
    summary = admin_get("/admin/summary")
    if not summary:
        return
    labels = [
        ("Total Users", "total_users"),
        ("Tenants", "total_tenants"),
        ("Leads", "total_leads"),
        ("Jobs", "total_jobs"),
        ("Emails Sent", "total_emails_sent"),
        ("Replies", "total_replies"),
        ("Free Users", "free_users"),
        ("Pro Users", "pro_users"),
        ("Agency Users", "agency_users"),
        ("Active Today", "active_users_today"),
        ("Leads Today", "leads_generated_today"),
        ("Failed Jobs", "failed_jobs"),
    ]
    st.markdown('<div class="page-section page-card"><div class="page-card-inner">', unsafe_allow_html=True)
    for start in range(0, len(labels), 4):
        columns = st.columns(4)
        for column, (label, key) in zip(columns, labels[start:start + 4]):
            column.metric(label, int(summary.get(key, 0) or 0))
    st.caption(
        f"Queued jobs: {int(summary.get('queued_jobs', 0) or 0)} | "
        f"Running jobs: {int(summary.get('running_jobs', 0) or 0)} | "
        f"Failed jobs: {int(summary.get('failed_jobs', 0) or 0)}"
    )
    st.markdown("</div></div>", unsafe_allow_html=True)


def render_admin_tables() -> None:
    users_payload = admin_get("/admin/users")
    usage_payload = admin_get("/admin/tenants/usage")
    leads_payload = admin_get("/admin/leads/recent")
    jobs_payload = admin_get("/admin/jobs/recent")
    users = pd.DataFrame(users_payload.get("items", []))
    usage = pd.DataFrame(usage_payload.get("items", []))
    leads = pd.DataFrame(leads_payload.get("items", []))
    jobs = pd.DataFrame(jobs_payload.get("items", []))

    plan_options = sorted(set(usage.get("plan", pd.Series(dtype=str)).dropna().astype(str).tolist()))
    selected_plans = st.multiselect("Plan filter", plan_options, default=plan_options)
    if selected_plans and not usage.empty:
        usage = usage[usage["plan"].isin(selected_plans)]
    if selected_plans and not users.empty and "plan" in users.columns:
        users = users[users["plan"].isin(selected_plans)]

    job_status_options = sorted(set(jobs.get("status", pd.Series(dtype=str)).dropna().astype(str).tolist()))
    selected_statuses = st.multiselect("Job status filter", job_status_options, default=job_status_options)
    if selected_statuses and not jobs.empty:
        jobs = jobs[jobs["status"].isin(selected_statuses)]

    tab_users, tab_usage, tab_leads, tab_jobs = st.tabs(["Recent Users", "Tenant Usage", "Recent Leads", "Recent Jobs"])
    with tab_users:
        st.dataframe(users, use_container_width=True, hide_index=True)
    with tab_usage:
        st.dataframe(usage, use_container_width=True, hide_index=True)
    with tab_leads:
        st.dataframe(leads, use_container_width=True, hide_index=True)
    with tab_jobs:
        st.dataframe(jobs, use_container_width=True, hide_index=True)


def render_admin_page() -> None:
    if not is_admin_user():
        st.error("Admin panel is available only for admin users.")
        return
    render_admin_summary()
    render_admin_tables()
    st.divider()
    render_admin_payments()


def main() -> None:
    bootstrap_dashboard_state()
    tenant = require_login()
    if tenant is None:
        return
    render_landing_styles()
    st.markdown('<div class="app-shell">', unsafe_allow_html=True)
    if render_plan_onboarding():
        st.markdown("</div>", unsafe_allow_html=True)
        return

    st.sidebar.markdown("### Workspace")
    if st.sidebar.button("Logout", use_container_width=True):
        st.session_state["auth"] = {}
        st.session_state["latest_subscription"] = {}
        st.session_state["plan_onboarding_seen"] = ""
        st.rerun()
    refresh_enabled = st.sidebar.checkbox("Auto-refresh", value=True)
    if refresh_enabled and st_autorefresh:
        st_autorefresh(interval=30_000, key="dashboard_refresh")
    st.sidebar.markdown("### Navigation")
    pages = ["Dashboard", "Billing", "Live Leads", "Replies", "Outreach Logs", "Settings"]
    if is_admin_user():
        pages.append("Admin")
    page = st.sidebar.radio("Page", pages, label_visibility="collapsed")

    render_dashboard_header()
    if page == "Dashboard":
        snapshot_response = api_request("GET", "/dashboard/snapshot")
        snapshot = snapshot_response.json()
        if not snapshot_response.is_success:
            st.error(str(snapshot.get("detail", "Could not load dashboard snapshot.")))
            st.markdown("</div>", unsafe_allow_html=True)
            return
        frame = load_dashboard_data(tenant)
        render_generation_status_card(snapshot)
        render_actions(tenant)
        render_dashboard_page(frame, snapshot)
    elif page == "Billing":
        render_billing_page()
    elif page == "Live Leads":
        frame = filtered_frame(load_dashboard_data(tenant))
        render_table_page(frame, STANDARD_LEAD_EXPORT_COLUMNS)
    elif page == "Replies":
        frame = load_dashboard_data(tenant)
        render_table_page(frame, ["company_url", "verified_email", "country", "reply_status", "last_reply_at"])
    elif page == "Outreach Logs":
        frame = load_dashboard_data(tenant)
        render_table_page(frame, ["company_url", "country", "industry", "outreach_status", "followup_count", "reply_status", "last_reply_at"])
    elif page == "Admin":
        render_admin_page()
    else:
        render_settings_page()
    st.markdown("</div>", unsafe_allow_html=True)


if __name__ == "__main__":
    main()
