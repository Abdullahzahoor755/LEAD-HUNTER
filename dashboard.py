"""
Streamlit analytics dashboard backed by PostgreSQL.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import html
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
from app.services.outreach_errors import outreach_error_message

try:
    from streamlit_autorefresh import st_autorefresh
except ImportError:
    st_autorefresh = None


PRODUCT_NAME = "Lead Hunter AI"
PRODUCT_TAGLINE = "Find leads, create agency pitches, and launch marketing campaigns."
LOCKED_FEATURE_MESSAGE = "This feature is available in Pro plan."
UNKNOWN_OUTREACH_FAILURE_MESSAGE = "This failed before detailed diagnostics were enabled. Re-run outreach to get the exact reason."
NO_VERIFIED_EMAIL_LEADS_MESSAGE = "No verified email leads found. Email outreach needs verified_email. Use WhatsApp Sales Kit for phone leads, or add a verified email."

st.set_page_config(page_title=PRODUCT_NAME, page_icon="🔎", layout="wide", initial_sidebar_state="expanded")

API_BASE_URL = os.getenv("APP_API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
DEFAULT_PAYMENT_QR_PATH = str(Path(__file__).resolve().parent / "assets" / "payment-qr.jpeg")
BOLT_LOGO_PATH = Path(__file__).resolve().parent / "assets" / "logo-bolt.png"


def resolve_local_asset_path(path_value: str) -> Path:
    path = Path(str(path_value or "").strip())
    if path.is_absolute():
        return path
    return Path(__file__).resolve().parent / path


def asset_data_uri(path: Path) -> str:
    if not path.exists():
        return ""
    mime_type = mimetypes.guess_type(str(path))[0] or "image/png"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def build_dashboard_search_query(niche: str, location: str) -> str:
    clean_niche = str(niche or "").strip()
    clean_location = str(location or "").strip()
    if not clean_niche and not clean_location:
        return ""
    if clean_niche and clean_location:
        lowered = clean_niche.lower()
        if lowered in {"it", "i.t", "information technology"}:
            return f"IT companies in {clean_location}"
        if "software" in lowered:
            return f"software houses in {clean_location}"
        if "digital marketing" in lowered:
            return f"digital marketing agencies in {clean_location}"
        return f"{clean_niche} companies in {clean_location}"
    return clean_niche or clean_location


def bootstrap_dashboard_state() -> None:
    st.session_state.setdefault("campaigns", [])
    st.session_state.setdefault("auth", {})
    st.session_state.setdefault("latest_subscription", {})
    st.session_state.setdefault("plan_onboarding_seen", "")
    st.session_state.setdefault("lead_generation_busy", False)
    st.session_state.setdefault("active_lead_generation_job_id", "")
    st.session_state.setdefault("marketing_campaign_kit", {})
    st.session_state.setdefault("marketing_campaign_source", "")
    st.session_state.setdefault("offer_match", {})
    st.session_state.setdefault("offer_match_source", "")
    st.session_state.setdefault("whatsapp_sales_kit", {})
    st.session_state.setdefault("whatsapp_sales_source", "")
    st.session_state.setdefault("mini_agency_plan", {})
    st.session_state.setdefault("auth_validated_token", "")


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
        "price": "$20/month",
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
        "price": "$30/month",
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
    return normalize_plan_label(str(auth.get("subscription_plan") or auth.get("plan") or ""))


def normalize_auth_role(value: Any) -> str:
    if isinstance(value, bool):
        return "admin" if value else ""
    return str(value or "").strip().lower()


def extract_auth_role(auth: Dict[str, Any], include_token: bool = True) -> str:
    if not isinstance(auth, dict):
        return ""

    for key in ("role", "user_role"):
        role = normalize_auth_role(auth.get(key))
        if role:
            return role

    user = auth.get("user")
    if isinstance(user, dict):
        role = normalize_auth_role(user.get("role"))
        if role:
            return role

    is_admin = auth.get("is_admin", auth.get("admin"))
    if isinstance(is_admin, bool):
        return "admin" if is_admin else ""
    if str(is_admin or "").strip().lower() in {"1", "true", "yes", "admin"}:
        return "admin"

    if include_token:
        token = str(auth.get("token", "") or "").strip()
        if token:
            try:
                return extract_auth_role(decode_jwt_token(token), include_token=False)
            except Exception:
                return ""
    return ""


def normalize_auth_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    auth = dict(payload or {}) if isinstance(payload, dict) else {}
    token = str(auth.get("token") or auth.get("auth_token") or "").strip()
    if token:
        auth["token"] = token
        try:
            token_payload = decode_jwt_token(token)
        except Exception:
            token_payload = {}
        for key in ("tenant_id", "tenant_slug", "user_id", "email", "role", "plan"):
            value = token_payload.get(key)
            if value:
                auth.setdefault(key, value)
    role = extract_auth_role(auth)
    if role:
        auth["role"] = role
    plan = str(auth.get("plan") or auth.get("subscription_plan") or "").strip()
    if plan:
        auth.setdefault("plan", plan)
        auth.setdefault("subscription_plan", plan)
    return auth


def query_param_value(params: Any, key: str) -> str:
    try:
        value = params.get(key, "")
    except Exception:
        return ""
    if isinstance(value, list):
        return str(value[0] if value else "")
    return str(value or "")


def current_query_params() -> Dict[str, Any]:
    try:
        return dict(getattr(st, "query_params"))
    except Exception:
        try:
            return dict(st.experimental_get_query_params())
        except Exception:
            return {}


def clear_google_auth_query_params() -> None:
    try:
        st.query_params.clear()
        return
    except Exception:
        pass
    try:
        st.experimental_set_query_params()
    except Exception:
        return


def consume_google_auth_redirect() -> None:
    params = current_query_params()
    status = query_param_value(params, "google_auth").strip().lower()
    if not status:
        return
    if status == "success":
        token = query_param_value(params, "auth_token").strip()
        if token:
            st.session_state["auth"] = normalize_auth_payload({"token": token})
            st.session_state["latest_subscription"] = {}
            st.session_state["plan_onboarding_seen"] = ""
            clear_google_auth_query_params()
            persist_auth_to_query(st.session_state["auth"])
            st.rerun()
            return
        st.session_state["google_auth_error"] = "Google sign in did not return a session token."
    elif status == "error":
        st.session_state["google_auth_error"] = query_param_value(params, "message") or "Google sign in failed safely."
    clear_google_auth_query_params()


def persist_auth_to_query(auth: Dict[str, Any]) -> None:
    token = str(auth.get("token", "") or "").strip()
    if not token:
        return
    try:
        st.query_params["auth_token"] = token
    except Exception:
        try:
            st.experimental_set_query_params(auth_token=token)
        except Exception:
            return


def hydrate_auth_from_query() -> None:
    auth = st.session_state.get("auth", {}) or {}
    if auth.get("token"):
        return
    token = query_param_value(current_query_params(), "auth_token").strip()
    if token:
        st.session_state["auth"] = normalize_auth_payload({"token": token})


def clear_auth_state() -> None:
    st.session_state["auth"] = {}
    st.session_state["latest_subscription"] = {}
    st.session_state["plan_onboarding_seen"] = ""
    st.session_state["auth_validated_token"] = ""
    try:
        if "auth_token" in st.query_params:
            del st.query_params["auth_token"]
    except Exception:
        pass


def validate_current_auth() -> bool:
    auth = st.session_state.get("auth", {}) or {}
    token = str(auth.get("token", "") or "").strip()
    if not token:
        return False
    if st.session_state.get("auth_validated_token") == token and auth.get("tenant_id"):
        return True
    try:
        response = api_request("GET", "/dashboard/snapshot", timeout=12)
    except Exception:
        return bool(auth.get("tenant_id"))
    if response.status_code in {401, 403}:
        clear_auth_state()
        return False
    if not response.is_success:
        return bool(auth.get("tenant_id"))
    payload = parse_api_json(response)
    normalized = normalize_auth_payload(auth)
    if payload.get("tenant_id"):
        normalized["tenant_id"] = str(payload.get("tenant_id"))
    st.session_state["auth"] = normalized
    st.session_state["auth_validated_token"] = token
    persist_auth_to_query(normalized)
    return bool(normalized.get("tenant_id"))


def start_google_auth_flow() -> None:
    try:
        response = api_request("GET", "/auth/google/start")
        payload = response.json()
    except Exception as error:
        st.error(f"Google sign in failed safely: {error}")
        return
    if not response.is_success:
        st.error(str(payload.get("detail", "Google sign in is not configured.")))
        return
    authorization_url = str(payload.get("authorization_url", "") or "").strip()
    if not authorization_url:
        st.error("Google sign in did not return an authorization URL.")
        return
    st.markdown(f'<meta http-equiv="refresh" content="0; url={html.escape(authorization_url)}">', unsafe_allow_html=True)
    if hasattr(st, "link_button"):
        st.link_button("Open Google Sign In", authorization_url, use_container_width=True)
    else:
        st.markdown(f'<a href="{html.escape(authorization_url)}">Open Google Sign In</a>', unsafe_allow_html=True)


def render_landing_styles() -> None:
    st.markdown(
        """
        <style>
        :root {
            --page-max-width: 1160px;
            --surface: #ffffff;
            --surface-strong: #ffffff;
            --surface-soft: #f8fafc;
            --line: #d6eef7;
            --line-strong: #bdebf7;
            --text: #16324f;
            --text-strong: #0b2545;
            --muted: #64748b;
            --accent: #22d3ee;
            --accent-2: #ef4b5f;
            --text-primary: #0b1220;
            --text-secondary: #334155;
            --text-muted: #475569;
            --heading: #071a2f;
            --card-gradient: linear-gradient(145deg, #eefaff 0%, #ffffff 72%);
            --soft-shadow: 0 18px 45px rgba(34, 211, 238, .13), 0 8px 22px rgba(11, 37, 69, .05);
        }
        .stApp {
            color: var(--text);
            background:
                linear-gradient(180deg, rgba(238, 250, 255, .85), rgba(255, 255, 255, 0) 260px),
                #ffffff;
        }
        .block-container {
            max-width: var(--page-max-width);
            padding-top: 2.6rem;
            padding-bottom: 2.5rem;
            padding-left: 1.6rem;
            padding-right: 1.6rem;
        }
        [data-testid="stHeader"] {
            background: transparent;
        }
        [data-testid="stDecoration"] {display: none;}
        [data-testid="stSidebarCollapseButton"],
        [data-testid="stSidebarCollapseButton"] button,
        [data-testid="collapsedControl"],
        [data-testid="collapsedControl"] button,
        [data-testid="stSidebarCollapsedControl"] button,
        button[title="Collapse sidebar"],
        button[title="Open sidebar"],
        button[aria-label="Collapse sidebar"],
        button[aria-label="Open sidebar"],
        [data-testid="stHeader"] button[aria-label*="sidebar" i],
        [data-testid="stHeader"] button[title*="sidebar" i],
        button[aria-label*="sidebar" i],
        button[title*="sidebar" i],
        [data-testid="collapsedControl"] button,
        [data-testid="stSidebarCollapsedControl"] button {
            display: inline-flex !important;
            align-items: center !important;
            justify-content: center !important;
            visibility: visible !important;
            opacity: 1 !important;
            pointer-events: auto !important;
            width: 2.4rem !important;
            height: 2.4rem !important;
            min-width: 2.4rem !important;
            min-height: 2.4rem !important;
            margin: .35rem !important;
            border: 1px solid rgba(255, 255, 255, .16) !important;
            border-radius: 11px !important;
            background: #07111f !important;
            color: #ffffff !important;
            box-shadow: 0 10px 24px rgba(7, 17, 31, .28) !important;
            z-index: 1000001 !important;
        }
        [data-testid="stSidebarCollapseButton"]:hover,
        [data-testid="stSidebarCollapseButton"] button:hover,
        [data-testid="collapsedControl"]:hover,
        [data-testid="collapsedControl"] button:hover,
        [data-testid="stSidebarCollapsedControl"] button:hover,
        button[title="Collapse sidebar"]:hover,
        button[title="Open sidebar"]:hover,
        button[aria-label="Collapse sidebar"]:hover,
        button[aria-label="Open sidebar"]:hover,
        [data-testid="stHeader"] button[aria-label*="sidebar" i]:hover,
        [data-testid="stHeader"] button[title*="sidebar" i]:hover,
        button[aria-label*="sidebar" i]:hover,
        button[title*="sidebar" i]:hover {
            background: #102238 !important;
            color: #ffffff !important;
            border-color: rgba(255, 255, 255, .24) !important;
            box-shadow: 0 12px 28px rgba(7, 17, 31, .34) !important;
        }
        [data-testid="stSidebarCollapseButton"] svg,
        [data-testid="stSidebarCollapseButton"] button svg,
        [data-testid="collapsedControl"] svg,
        [data-testid="collapsedControl"] button svg,
        [data-testid="stSidebarCollapsedControl"] button svg,
        button[title="Collapse sidebar"] svg,
        button[title="Open sidebar"] svg,
        button[aria-label="Collapse sidebar"] svg,
        button[aria-label="Open sidebar"] svg,
        [data-testid="stHeader"] button[aria-label*="sidebar" i] svg,
        [data-testid="stHeader"] button[title*="sidebar" i] svg,
        button[aria-label*="sidebar" i] svg,
        button[title*="sidebar" i] svg,
        [data-testid="collapsedControl"] button svg,
        [data-testid="stSidebarCollapsedControl"] button svg {
            color: #ffffff !important;
            fill: currentColor !important;
            stroke: currentColor !important;
        }
        [data-testid="stSidebar"] {
            background: #f8fafc;
            border-right: 1px solid #e6f4f8;
        }
        [data-testid="stSidebar"][aria-expanded="true"] {
            min-width: 18rem;
            max-width: min(18rem, 86vw);
        }
        [data-testid="stSidebar"] [data-testid="stSidebarContent"] {
            padding: 1.1rem .9rem;
        }
        .lhai-sidebar-account-menu {
            margin-top: 2rem;
            padding-top: .85rem;
            border-top: 1px solid #dceff6;
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
        .lhai-sidebar-brand {
            padding: .15rem .1rem .85rem;
            margin-bottom: .65rem;
            border-bottom: 1px solid #dceff6;
        }
        .lhai-sidebar-brand-inner {
            display: flex;
            align-items: center;
            gap: .68rem;
        }
        .lhai-sidebar-logo-wrap {
            width: 40px;
            height: 40px;
            flex: 0 0 40px;
            border-radius: 50%;
            display: grid;
            place-items: center;
            background: transparent;
            overflow: hidden;
            box-shadow: 0 8px 22px rgba(7, 17, 31, .16);
        }
        .lhai-sidebar-logo {
            width: 38px;
            height: 38px;
            border-radius: 50%;
            object-fit: cover;
            display: block;
        }
        .lhai-sidebar-copy {
            min-width: 0;
        }
        .lhai-sidebar-title {
            color: #0b2545;
            font-weight: 850;
            font-size: 1.05rem;
            line-height: 1.2;
            letter-spacing: 0;
        }
        .lhai-sidebar-subtitle {
            color: #475569;
            font-size: .78rem;
            line-height: 1.35;
            margin-top: .18rem;
        }
        .app-shell {
            max-width: var(--page-max-width);
            margin: 0 auto;
        }
        .app-footer {
            margin: 1.4rem 0 .45rem;
            padding: .85rem 0 0;
            border-top: 1px solid var(--line);
            color: var(--muted);
            font-size: .78rem;
            text-align: center;
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
        .soft-card {
            border: 1px solid var(--line);
            border-radius: 8px;
            background: rgba(15, 23, 42, .66);
            padding: .9rem;
            min-width: 0;
        }
        .soft-card h4 {
            margin: 0 0 .4rem;
            color: var(--text-strong);
            font-size: .98rem;
        }
        .soft-card p {
            color: var(--muted);
            line-height: 1.55;
            margin: 0;
        }
        .metric-card-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
            gap: .75rem;
        }
        .metric-card {
            border: 1px solid var(--line);
            border-radius: 8px;
            background: rgba(15, 23, 42, .72);
            padding: .85rem;
            min-width: 0;
        }
        .metric-label {color: var(--muted); font-size: .78rem; margin: 0 0 .3rem;}
        .metric-value {color: var(--text-strong); font-size: 1.18rem; font-weight: 850; margin: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;}
        .metric-help {color: #cbd5e1; font-size: .8rem; margin: .28rem 0 0; line-height: 1.45;}
        .status-badge {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            border-radius: 999px;
            padding: .25rem .65rem;
            border: 1px solid rgba(148, 163, 184, .2);
            background: rgba(15, 23, 42, .72);
            color: #dbeafe;
            font-size: .78rem;
            font-weight: 800;
        }
        .status-badge.success {border-color: rgba(34, 197, 94, .28); color: #bbf7d0; background: rgba(20, 83, 45, .22);}
        .status-badge.warning {border-color: rgba(251, 191, 36, .28); color: #fde68a; background: rgba(113, 63, 18, .2);}
        .empty-state {
            border: 1px dashed rgba(148, 163, 184, .26);
            border-radius: 8px;
            background: rgba(15, 23, 42, .42);
            padding: 1rem;
            color: var(--text);
        }
        .empty-state h4 {margin: 0 0 .3rem; color: var(--text-strong);}
        .empty-state p {margin: 0; color: var(--muted); line-height: 1.55;}
        .script-block {
            border: 1px solid rgba(148, 163, 184, .18);
            border-radius: 8px;
            background: rgba(2, 6, 23, .5);
            padding: .8rem;
            color: #dbeafe;
            line-height: 1.6;
            white-space: pre-wrap;
            overflow-wrap: anywhere;
        }
        .mini-card-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
            gap: .8rem;
        }
        .day-card {
            border: 1px solid var(--line);
            border-radius: 8px;
            background: rgba(15, 23, 42, .66);
            padding: .85rem;
            min-width: 0;
        }
        .day-card strong {color: var(--text-strong);}
        .day-card p {color: var(--muted); margin: .35rem 0; line-height: 1.5;}
        .day-card li {color: var(--text); margin: .25rem 0; line-height: 1.45;}
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
            margin-top: .1rem;
            margin-bottom: .9rem;
            border: 1px solid var(--line);
            border-radius: 8px;
            background: rgba(15, 23, 42, .78);
            padding: .95rem 1rem;
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
            border-radius: 12px;
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
            font-size: clamp(2rem, 4vw, 3.8rem);
            line-height: 1.02;
            font-weight: 900;
            letter-spacing: 0;
            margin: .1rem 0 1rem;
            background: linear-gradient(92deg, #f8fafc 0%, #67e8f9 42%, #c084fc 78%, #f0abfc 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        .landing-tagline {
            font-size: 1.05rem;
            line-height: 1.65;
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
            max-width: 420px;
            margin-left: auto;
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
        .marketing-grid {
            display: grid;
            grid-template-columns: repeat(3, minmax(220px, 1fr));
            gap: .8rem;
            margin: 1rem 0;
        }
        .marketing-card {
            border: 1px solid var(--line);
            border-radius: 8px;
            background: rgba(15, 23, 42, .66);
            padding: 1rem;
        }
        .marketing-card h4 {
            margin: 0 0 .4rem;
            color: var(--text-strong);
            font-size: 1rem;
        }
        .marketing-card p {
            margin: 0;
            color: var(--muted);
            line-height: 1.5;
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
        .stButton > button:disabled,
        [data-testid="stFormSubmitButton"] button:disabled,
        .stDownloadButton button:disabled {
            opacity: .58;
            border: 1px solid rgba(148, 163, 184, .22) !important;
            background: rgba(30, 41, 59, .72) !important;
            color: #cbd5e1 !important;
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
        [data-testid="stDataFrame"] {
            border: 1px solid var(--line);
            border-radius: 8px;
            overflow: hidden;
            background: rgba(15, 23, 42, .55);
        }
        div[data-testid="stMetric"] {
            background: rgba(15, 23, 42, .42);
            border: 1px solid rgba(148, 163, 184, .14);
            border-radius: 8px;
            padding: .7rem;
        }
        .stTabs [data-baseweb="tab-list"] {
            gap: .4rem;
        }
        .stTabs [data-baseweb="tab"] {
            border-radius: 8px;
            border: 1px solid rgba(148, 163, 184, .16);
            background: rgba(15, 23, 42, .46);
        }
        /* Global light SaaS theme overrides */
        .stApp,
        .main,
        section.main,
        [data-testid="stAppViewContainer"],
        [data-testid="stVerticalBlock"] {
            color: var(--text);
        }
        h1, h2, h3, h4, h5, h6,
        p, label, span, li,
        [data-testid="stMarkdownContainer"] {
            color: var(--text);
        }
        [data-testid="stHeader"] {
            background: rgba(255, 255, 255, .86);
            backdrop-filter: blur(14px);
            border-bottom: 1px solid rgba(214, 238, 247, .72);
        }
        [data-testid="stSidebar"] {
            background: linear-gradient(180deg, #f8fafc 0%, #ffffff 100%) !important;
            border-right: 1px solid #dceff6 !important;
            box-shadow: 14px 0 34px rgba(11, 37, 69, .035);
        }
        [data-testid="stSidebar"] [data-testid="stSidebarContent"] {
            background: transparent;
        }
        [data-testid="stSidebar"] .lhai-sidebar-account-menu {
            margin-top: min(22vh, 9rem);
            border-top: 1px solid #dceff6;
        }
        [data-testid="stSidebar"] label,
        [data-testid="stSidebar"] p,
        [data-testid="stSidebar"] span,
        [data-testid="stSidebar"] .stMarkdown {
            color: var(--text) !important;
        }
        [data-testid="stSidebar"] h1,
        [data-testid="stSidebar"] h2,
        [data-testid="stSidebar"] h3 {
            color: var(--text-strong) !important;
        }
        [data-testid="stSidebar"] .stRadio [role="radio"] {
            border-radius: 16px;
            border: 1px solid transparent;
            background: transparent;
            color: var(--text);
        }
        [data-testid="stSidebar"] .stRadio [role="radio"][aria-checked="true"] {
            background: linear-gradient(135deg, #eefaff 0%, #ffffff 100%);
            border-color: var(--line);
            box-shadow: 0 10px 24px rgba(34, 211, 238, .12);
        }
        [data-testid="stSidebar"] .stRadio [role="radio"][aria-checked="true"] p,
        [data-testid="stSidebar"] .stRadio [role="radio"][aria-checked="true"] span {
            color: #0f7490 !important;
            font-weight: 750;
        }
        .lhai-sidebar-brand {border-bottom-color: #dceff6;}
        .lhai-sidebar-title {color: var(--text-strong);}
        .lhai-sidebar-subtitle {color: var(--muted);}
        .page-card,
        .metric-card,
        .soft-card,
        .panel-card,
        .marketing-card,
        .day-card,
        .top-hero,
        .landing-shell,
        .feature-card,
        .auth-card,
        .plan-card,
        .empty-state,
        div[data-testid="stMetric"] {
            border: 1px solid var(--line) !important;
            border-radius: 20px !important;
            background: var(--card-gradient) !important;
            box-shadow: var(--soft-shadow) !important;
            color: var(--text) !important;
        }
        .page-card-inner {
            color: var(--text);
        }
        .top-hero {
            box-shadow: 0 20px 48px rgba(34, 211, 238, .12), 0 8px 22px rgba(11, 37, 69, .05) !important;
        }
        .landing-shell:before {
            background: linear-gradient(110deg, rgba(34, 211, 238, .16), transparent 48%, rgba(239, 75, 95, .10));
            filter: blur(30px);
        }
        .hero-panel,
        .hero-stack {
            background: transparent !important;
            box-shadow: none !important;
        }
        .page-title,
        .hero-title,
        .admin-title,
        .main-title,
        .section-title,
        .metric-value,
        .hero-metric .value,
        .panel-card h3,
        .panel-card h4,
        .soft-card h4,
        .marketing-card h4,
        .empty-state h4,
        .feature-card h3,
        .auth-card h2,
        .plan-price,
        .price-chip strong {
            color: var(--text-strong) !important;
            letter-spacing: 0 !important;
        }
        .page-subtitle,
        .hero-note,
        .main-subtitle,
        .section-caption,
        .settings-note,
        .metric-label,
        .metric-help,
        .hero-metric .label,
        .hero-metric .hint,
        .summary-stack,
        .soft-card p,
        .marketing-card p,
        .feature-card p,
        .auth-card p,
        .empty-state p,
        .day-card p {
            color: var(--muted) !important;
        }
        .hero-headline {
            background: linear-gradient(92deg, #0b2545 0%, #0891b2 52%, #ef4b5f 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        .landing-title,
        .hero-kicker {
            color: #0f7490 !important;
        }
        .hero-kicker,
        .plan-badge,
        .status-badge {
            border-color: var(--line-strong) !important;
            background: linear-gradient(135deg, #eefaff 0%, #ffffff 100%) !important;
            color: #0f7490 !important;
            box-shadow: 0 8px 20px rgba(34, 211, 238, .10);
        }
        .status-badge.success {
            border-color: rgba(34, 211, 238, .35) !important;
            color: #0f7490 !important;
            background: #ecfeff !important;
        }
        .status-badge.warning,
        .premium-badge {
            border-color: rgba(239, 75, 95, .24) !important;
            color: #b4233b !important;
            background: #fff1f3 !important;
        }
        .script-block {
            border: 1px solid var(--line);
            border-radius: 18px;
            background: linear-gradient(145deg, #f8fdff 0%, #ffffff 100%);
            color: var(--text);
            box-shadow: inset 0 1px 0 rgba(255, 255, 255, .85);
        }
        .cta-pill,
        .price-chip {
            border-color: var(--line);
            color: var(--text);
            background: #ffffff;
            box-shadow: 0 10px 22px rgba(11, 37, 69, .04);
        }
        .cta-pill.primary,
        .dashboard-actions .stButton > button,
        .auth-card .stButton > button,
        .auth-card [data-testid="stFormSubmitButton"] button {
            color: #06313e !important;
            background: linear-gradient(90deg, #22d3ee, #a7f3ff) !important;
            border: 1px solid rgba(34, 211, 238, .38) !important;
            box-shadow: 0 14px 32px rgba(34, 211, 238, .20) !important;
        }
        .stButton > button,
        [data-testid="stFormSubmitButton"] button,
        .stDownloadButton button,
        .stLinkButton a {
            border-radius: 16px !important;
            border: 1px solid #b9dce7 !important;
            background: #ffffff !important;
            color: var(--text-strong) !important;
            box-shadow: 0 8px 18px rgba(11, 37, 69, .045) !important;
        }
        .stButton > button:hover,
        [data-testid="stFormSubmitButton"] button:hover,
        .stDownloadButton button:hover,
        .stLinkButton a:hover {
            border-color: #22d3ee !important;
            color: #0f7490 !important;
            box-shadow: 0 12px 28px rgba(34, 211, 238, .16) !important;
        }
        .stButton > button:disabled,
        [data-testid="stFormSubmitButton"] button:disabled,
        .stDownloadButton button:disabled {
            opacity: .72;
            border-color: #dbeaf0 !important;
            background: #f8fafc !important;
            color: #94a3b8 !important;
            box-shadow: none !important;
        }
        .dashboard-actions .premium-action {
            border-color: rgba(239, 75, 95, .22);
            background: #fff1f3;
            color: #b4233b;
        }
        input,
        textarea,
        [data-baseweb="input"],
        [data-baseweb="textarea"],
        [data-baseweb="select"] > div,
        [data-testid="stTextInput"] input,
        [data-testid="stTextArea"] textarea,
        [data-testid="stNumberInput"] input,
        .dashboard-input input,
        .auth-card input {
            color: var(--text-strong) !important;
            background: #ffffff !important;
            border-color: #cfe8f1 !important;
            border-radius: 16px !important;
            box-shadow: inset 0 1px 0 rgba(255, 255, 255, .9), 0 8px 20px rgba(11, 37, 69, .035) !important;
        }
        input::placeholder,
        textarea::placeholder {
            color: #8aa1b5 !important;
        }
        [data-baseweb="select"] svg,
        [data-testid="stSelectbox"] svg,
        [data-testid="stMultiSelect"] svg {
            color: var(--text-strong) !important;
        }
        .stSlider [data-baseweb="slider"] div {
            color: #ef4b5f !important;
        }
        .stSlider [role="slider"] {
            background: #ef4b5f !important;
            border-color: #ef4b5f !important;
            box-shadow: 0 0 0 4px rgba(239, 75, 95, .12) !important;
        }
        [data-testid="stDataFrame"] {
            border: 1px solid var(--line);
            border-radius: 20px;
            overflow: hidden;
            background: #ffffff;
            box-shadow: 0 14px 34px rgba(11, 37, 69, .055);
        }
        .stTabs [data-baseweb="tab-list"] {
            gap: .4rem;
            border-bottom-color: #e6f4f8;
        }
        .stTabs [data-baseweb="tab"] {
            border-radius: 16px;
            border: 1px solid var(--line);
            background: #ffffff;
            color: var(--text);
        }
        .stTabs [aria-selected="true"] {
            background: linear-gradient(135deg, #eefaff 0%, #ffffff 100%) !important;
            color: #0f7490 !important;
            border-color: var(--line-strong) !important;
        }
        .stAlert {
            border-radius: 18px !important;
            border-color: var(--line) !important;
            box-shadow: 0 10px 24px rgba(11, 37, 69, .04);
        }
        .locked-action,
        .day-card li,
        .panel-card p,
        .panel-card li,
        .panel-card caption {
            color: var(--text) !important;
        }
        /* Login/home contrast pass: keep light theme and layout, make typography readable. */
        .landing-shell,
        .auth-card,
        .feature-card,
        .price-chip,
        .plan-card {
            color: var(--text-primary) !important;
            opacity: 1 !important;
        }
        .hero-headline {
            color: var(--heading) !important;
            background: none !important;
            -webkit-background-clip: initial !important;
            background-clip: initial !important;
            -webkit-text-fill-color: var(--heading) !important;
            text-shadow: none !important;
            opacity: 1 !important;
        }
        .landing-title,
        .auth-card h2,
        .feature-card h3,
        .price-chip strong,
        .plan-card strong,
        .plan-price {
            color: var(--heading) !important;
            -webkit-text-fill-color: var(--heading) !important;
            opacity: 1 !important;
        }
        .landing-tagline,
        .landing-shell p,
        .landing-shell li,
        .feature-card p,
        .price-chip,
        .plan-card,
        .plan-card p,
        .auth-card p,
        .auth-card + div,
        .auth-card ~ div {
            color: var(--text-secondary) !important;
            opacity: 1 !important;
        }
        .hero-kicker,
        .cta-pill,
        .auth-card label,
        .auth-card [data-testid="stTextInput"] label,
        .auth-card [data-testid="stTextInput"] label p,
        .auth-card [data-testid="stTextInput"] label span,
        .auth-card [data-testid="stForm"] label,
        .auth-card [data-testid="stForm"] label p,
        .auth-card [data-testid="stForm"] label span,
        .auth-card .stTabs [data-baseweb="tab"],
        .auth-card .stTabs [data-baseweb="tab"] p,
        .auth-card .stTabs [data-baseweb="tab"] span {
            color: var(--text-primary) !important;
            -webkit-text-fill-color: var(--text-primary) !important;
            opacity: 1 !important;
        }
        .auth-card .stTabs [aria-selected="true"],
        .auth-card .stTabs [aria-selected="true"] p,
        .auth-card .stTabs [aria-selected="true"] span {
            color: var(--heading) !important;
            -webkit-text-fill-color: var(--heading) !important;
            font-weight: 800;
        }
        .auth-card input,
        .auth-card textarea,
        .auth-card [data-testid="stTextInput"] input,
        .auth-card [data-testid="stTextArea"] textarea,
        .auth-card [data-testid="stForm"] input,
        .auth-card [data-testid="stForm"] textarea {
            color: var(--text-primary) !important;
            -webkit-text-fill-color: var(--text-primary) !important;
            opacity: 1 !important;
        }
        .auth-card input::placeholder,
        .auth-card textarea::placeholder {
            color: #94a3b8 !important;
            -webkit-text-fill-color: #94a3b8 !important;
            opacity: 1 !important;
        }
        .auth-card [data-testid="stMarkdownContainer"],
        .landing-shell [data-testid="stMarkdownContainer"] {
            color: var(--text-secondary) !important;
            opacity: 1 !important;
        }
        img {
            max-width: 100%;
            height: auto;
        }
        .stDataFrame,
        [data-testid="stDataFrame"],
        [data-testid="stTable"] {
            max-width: 100%;
            overflow-x: auto;
        }
        [data-testid="column"] {
            min-width: 0;
        }
        @media (max-width: 980px) {
            .block-container {
                max-width: 100%;
                padding-left: .85rem;
                padding-right: .85rem;
                padding-top: 2.8rem;
            }
            [data-testid="stSidebar"][aria-expanded="true"] {
                min-width: 0 !important;
                max-width: 86vw !important;
            }
            [data-testid="stSidebar"] [data-testid="stSidebarContent"] {
                padding: .9rem .75rem 1.25rem;
            }
            .top-hero {
                display: block;
                max-height: none;
                min-height: 0;
                padding: .85rem;
            }
            .hero-title,
            .page-title,
            .main-title,
            .admin-title {
                font-size: 1.35rem;
                line-height: 1.2;
                overflow-wrap: anywhere;
            }
            .hero-headline {
                font-size: 2rem;
                line-height: 1.08;
            }
            .landing-shell {
                border-radius: 8px;
                padding: 1rem;
                overflow: visible;
            }
            .auth-card {
                max-width: none;
                margin-left: 0;
                margin-top: 1rem;
                padding: 1rem;
            }
            .pricing-strip,
            .feature-grid,
            .section-grid,
            .metric-card-grid,
            .mini-card-grid,
            .hero-metrics,
            .locked-grid,
            .marketing-grid {
                grid-template-columns: 1fr !important;
            }
            .summary-stack {
                align-items: flex-start;
                text-align: left;
                margin-top: .85rem;
                max-width: 100%;
            }
            .summary-stack span,
            .metric-value,
            .hero-metric .value {
                white-space: normal;
                overflow-wrap: anywhere;
            }
            .stButton > button,
            [data-testid="stFormSubmitButton"] button,
            .stDownloadButton button,
            .stLinkButton a {
                min-height: 2.65rem;
                white-space: normal;
            }
        }
        @media (max-width: 640px) {
            .block-container {
                padding-left: .65rem;
                padding-right: .65rem;
            }
            .page-card-inner,
            .panel-card,
            .feature-card,
            .plan-card,
            .metric-card,
            .soft-card,
            .marketing-card,
            .day-card {
                padding: .8rem !important;
            }
            .hero-headline {
                font-size: 1.65rem;
            }
            .landing-tagline,
            .hero-note,
            .page-subtitle,
            .section-caption {
                font-size: .95rem;
                line-height: 1.5;
            }
            .cta-row {
                gap: .5rem;
            }
            .cta-pill {
                width: 100%;
                justify-content: center;
                padding: .68rem .8rem;
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
    timeout = kwargs.pop("timeout", 15)
    headers = dict(kwargs.pop("headers", {}) or {})
    headers.update(api_headers())
    with httpx.Client(timeout=timeout) as client:
        return client.request(method, f"{API_BASE_URL}{path}", headers=headers, **kwargs)


def get_auth_role() -> str:
    auth = st.session_state.get("auth", {}) or {}
    role = extract_auth_role(auth)
    if role:
        return role

    session_payload: Dict[str, Any] = {}
    for key in ("role", "user_role", "is_admin", "admin", "user"):
        value = st.session_state.get(key)
        if value not in (None, ""):
            session_payload[key] = value
    return extract_auth_role(session_payload, include_token=False)


def is_admin_user() -> bool:
    return get_auth_role() == "admin"


def render_page_header(title: str, subtitle: str, badge: str | None = None) -> None:
    badge_markup = f'<span class="plan-badge">{html.escape(badge)}</span>' if badge else ""
    st.markdown(
        f"""
        <div class="page-section">
            <h1 class="page-title">{html.escape(title)}</h1>
            <p class="page-subtitle">{html.escape(subtitle)}</p>
            {badge_markup}
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_metric_card(label: str, value: Any, help_text: str | None = None) -> str:
    help_markup = f'<p class="metric-help">{html.escape(str(help_text))}</p>' if help_text else ""
    return (
        '<div class="metric-card">'
        f'<p class="metric-label">{html.escape(str(label))}</p>'
        f'<p class="metric-value">{html.escape(str(value))}</p>'
        f"{help_markup}"
        "</div>"
    )


def render_section_card(title: str, body: str) -> None:
    st.markdown(
        f"""
        <div class="page-section page-card">
            <div class="page-card-inner">
                <div class="section-title">{html.escape(title)}</div>
                <div class="section-caption">{html.escape(body)}</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_empty_state(title: str, message: str, action_label: str | None = None) -> None:
    action = f'<p><span class="status-badge">{html.escape(action_label)}</span></p>' if action_label else ""
    st.markdown(
        f"""
        <div class="empty-state">
            <h4>{html.escape(title)}</h4>
            <p>{html.escape(message)}</p>
            {action}
        </div>
        """,
        unsafe_allow_html=True,
    )


def require_login() -> TenantContext | None:
    consume_google_auth_redirect()
    hydrate_auth_from_query()
    auth = st.session_state.get("auth", {})
    if auth.get("token") and validate_current_auth():
        auth = st.session_state.get("auth", {}) or {}
        return TenantContext(
            tenant_id=str(auth["tenant_id"]),
            user_id=str(auth.get("user_id", "")),
            metadata={"email": str(auth.get("email", ""))},
        )

    render_landing_styles()
    st.markdown('<div class="landing-shell">', unsafe_allow_html=True)
    hero_col, auth_col = st.columns([1.35, .9], gap="large")
    with hero_col:
        st.markdown('<div class="hero-kicker">AI Agency Operating System</div>', unsafe_allow_html=True)
        st.markdown(f'<div class="landing-title">{PRODUCT_NAME}</div>', unsafe_allow_html=True)
        st.markdown(f'<div class="hero-headline">{html.escape(PRODUCT_TAGLINE)}</div>', unsafe_allow_html=True)
        st.markdown(
            '<div class="landing-tagline">Use Email CRM to find and manage leads, then use the AI Marketing Kit to create pitches, ad copy, scripts, and campaign plans.</div>',
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
                <div class="price-chip"><strong>Free</strong>Fallback mode and CSV-ready lead workflow</div>
                <div class="price-chip"><strong>Pro - $20/month</strong>Outreach, Gmail, and reply workflow</div>
                <div class="price-chip"><strong>Agency - $30/month</strong>Higher limits and agency operating tools</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.markdown(
            """
            <div class="feature-grid">
                <div class="feature-card"><h3>Email CRM</h3><p>Find leads, review contacts, export CSVs, and manage outreach-ready records.</p></div>
                <div class="feature-card"><h3>AI Agency Kit</h3><p>Turn a lead into service offers, pitch angles, scripts, and proposal outlines.</p></div>
                <div class="feature-card"><h3>Marketing Campaign Kit</h3><p>Create ad copy, reels scripts, landing page copy, and content calendars.</p></div>
            </div>
            <div class="settings-note">Works free in fallback mode. Connect your own AI key for enhanced outputs.</div>
            """,
            unsafe_allow_html=True,
        )

    with auth_col:
        st.markdown('<div class="auth-card"><h2>Access your workspace</h2><p>Create a Free workspace or log into an existing tenant.</p>', unsafe_allow_html=True)
        google_error = str(st.session_state.pop("google_auth_error", "") or "").strip()
        if google_error:
            st.error(google_error)
        login_tab, signup_tab = st.tabs(["Login", "Create Free Account"])
        with login_tab:
            with st.form("admin_login"):
                tenant_id = st.text_input("Tenant ID", value="", placeholder="your-workspace")
                email = st.text_input("Email", value="", placeholder="you@company.com")
                password = st.text_input("Password", value="", type="password", placeholder="Enter your password")
                submitted = st.form_submit_button("Login", use_container_width=True)
            if st.button("Continue with Google", key="google_login_button", use_container_width=True):
                start_google_auth_flow()
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
                    st.session_state["auth"] = normalize_auth_payload(payload)
                    st.session_state["latest_subscription"] = {}
                    st.session_state["plan_onboarding_seen"] = ""
                    persist_auth_to_query(st.session_state["auth"])
                    st.rerun()
                except Exception as error:
                    st.error(str(error))
        with signup_tab:
            with st.form("signup_form"):
                signup_tenant = st.text_input("Workspace / Tenant ID", value="", placeholder="your-workspace")
                signup_name = st.text_input("Business Name", value="", placeholder="Acme Agency")
                signup_email = st.text_input("Work Email", value="", placeholder="you@company.com")
                signup_full_name = st.text_input("Your Name", value="", placeholder="Your name")
                signup_password = st.text_input("Password", value="", type="password", placeholder="Enter your password")
                signup_submitted = st.form_submit_button("Start Free", use_container_width=True)
            if st.button("Sign up with Google", key="google_signup_button", use_container_width=True):
                start_google_auth_flow()
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
                    st.session_state["auth"] = normalize_auth_payload(payload)
                    st.session_state["latest_subscription"] = {}
                    st.session_state["plan_onboarding_seen"] = ""
                    persist_auth_to_query(st.session_state["auth"])
                    st.rerun()
                except Exception as error:
                    st.error(str(error))
        st.markdown('</div>', unsafe_allow_html=True)
    render_app_footer()
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


def format_outreach_error(error_code: str) -> str:
    value = str(error_code or "").strip().lower()
    if value == "unknown_outreach_failure":
        return UNKNOWN_OUTREACH_FAILURE_MESSAGE
    return outreach_error_message(value)


def contact_readiness_label(verified_email: str, phone: str, likely_email: str = "") -> str:
    if str(verified_email or "").strip():
        return "Email-ready"
    if str(likely_email or "").strip():
        return "Likely email"
    if str(phone or "").strip():
        return "Phone-only"
    return "No contact"


def contact_next_action(verified_email: str, phone: str, likely_email: str = "") -> str:
    if contact_readiness_label(verified_email, phone, likely_email) == "Phone-only":
        return "Generate WhatsApp Sales Kit"
    return ""


def is_development_mode() -> bool:
    environment = str(os.getenv("APP_ENV") or os.getenv("ENVIRONMENT") or os.getenv("ENV") or "").strip().lower()
    debug_enabled = str(os.getenv("DEBUG", "") or "").strip().lower() in {"1", "true", "yes", "on"}
    return debug_enabled or environment in {"development", "dev", "local", "test"}


def can_show_test_lead_tool() -> bool:
    return is_admin_user() or is_development_mode()


STANDARD_LEAD_EXPORT_COLUMNS = [
    "company",
    "company_url",
    "created_at",
    "country",
    "verified_email",
    "phone",
    "likely_email",
    "email_confidence",
    "email_quality",
    "lead_quality_grade",
    "readiness",
    "contact_readiness",
    "lead_readiness_score",
    "next_contact_action",
    "service_reason",
    "industry",
    "score",
    "outreach_status",
    "outreach_error",
    "save_reason",
    "followup_count",
    "reply_status",
    "last_reply_at",
    "source_query",
]


@st.cache_data(ttl=45, show_spinner=False)
def load_dashboard_data(_tenant: TenantContext | None = None, limit: int = 50) -> pd.DataFrame:
    response = api_request("GET", "/leads?include_agency_kit=true")
    payload = response.json()
    if not response.is_success:
        raise RuntimeError(str(payload.get("detail", "Could not load leads.")))
    items = payload.get("items", [])
    rows = []
    for item in items[: max(1, int(limit or 50))]:
        rows.append(
            {
                "id": str(item.get("id", "") or "").strip(),
                "company": str(item.get("company", "") or item.get("company_name", "") or "").strip(),
                "company_url": str(item.get("company_url", "") or "").strip(),
                "created_at": str(item.get("created_at", "") or "").strip(),
                "country": str(item.get("country", "") or "").strip(),
                "verified_email": str(item.get("verified_email", "") or "").strip().lower(),
                "phone": str(item.get("phone", "") or "").strip(),
                "likely_email": str(item.get("likely_email", "") or "").strip().lower(),
                "email_confidence": str(item.get("email_confidence", "") or "").strip().lower(),
                "email_quality": str(item.get("email_quality", "") or "").strip().lower(),
                "lead_quality_grade": str(item.get("lead_quality_grade", "") or "").strip(),
                "readiness": str(item.get("readiness_label", "") or item.get("readiness", "") or "").strip(),
                "lead_readiness_score": item.get("lead_readiness_score", 0),
                "service_reason": str(item.get("service_reason", "") or "").strip(),
                "lead_reason": str(item.get("lead_reason", "") or item.get("service_reason", "") or "").strip(),
                "industry": str(item.get("industry", "") or "").strip(),
                "score": item.get("score", 0),
                "outreach_status": str(item.get("outreach_status", "") or "").strip().lower(),
                "outreach_error": format_outreach_error(str(item.get("outreach_error", "") or "").strip().lower()),
                "save_reason": str(
                    item.get("save_reason", "")
                    or item.get("service_reason", "")
                    or item.get("lead_reason", "")
                    or ""
                ).strip(),
                "followup_count": item.get("followup_count", 0),
                "reply_status": str(item.get("reply_status", "") or "").strip().lower(),
                "bounce_status": str(item.get("bounce_status", "") or "").strip().lower(),
                "whatsapp_ready": bool(item.get("whatsapp_ready", False)),
                "whatsapp_status": str(item.get("whatsapp_status", "not_contacted") or "not_contacted").strip().lower(),
                "whatsapp_message": str(item.get("whatsapp_message", "") or "").strip(),
                "whatsapp_last_contacted_at": str(item.get("whatsapp_last_contacted_at", "") or "").strip(),
                "last_reply_at": str(item.get("last_reply_at", "") or "").strip(),
                "source_query": str(item.get("source_query", "") or "").strip(),
                "agency_kit": item.get("agency_kit", {}) if isinstance(item.get("agency_kit", {}), dict) else {},
                "offer_match": item.get("offer_match", {}) if isinstance(item.get("offer_match", {}), dict) else {},
                "whatsapp_sales_kit": item.get("whatsapp_sales_kit", {}) if isinstance(item.get("whatsapp_sales_kit", {}), dict) else {},
                "marketing_campaign_kit": item.get("marketing_campaign_kit", {}) if isinstance(item.get("marketing_campaign_kit", {}), dict) else {},
            }
        )
        rows[-1]["readiness"] = {
            "email_ready": "Email Ready",
            "phone_ready": "Phone Ready",
            "research_needed": "Research Needed",
        }.get(str(rows[-1]["readiness"]).strip().lower(), rows[-1]["readiness"])
        rows[-1]["contact_readiness"] = contact_readiness_label(rows[-1]["verified_email"], rows[-1]["phone"], rows[-1]["likely_email"])
        rows[-1]["next_contact_action"] = contact_next_action(rows[-1]["verified_email"], rows[-1]["phone"], rows[-1]["likely_email"])
    if not rows:
        return pd.DataFrame(columns=["id", *STANDARD_LEAD_EXPORT_COLUMNS, "lead_reason", "agency_kit", "offer_match", "whatsapp_sales_kit", "marketing_campaign_kit"])
    frame = pd.DataFrame(rows)
    frame["score"] = pd.to_numeric(frame.get("score", pd.Series(0, index=frame.index)), errors="coerce").fillna(0)
    frame["lead_readiness_score"] = pd.to_numeric(frame.get("lead_readiness_score", pd.Series(0, index=frame.index)), errors="coerce").fillna(0).astype(int)
    frame["followup_count"] = pd.to_numeric(frame.get("followup_count", pd.Series(0, index=frame.index)), errors="coerce").fillna(0).astype(int)
    for column in STANDARD_LEAD_EXPORT_COLUMNS:
        if column not in frame.columns:
            frame[column] = ""
    return frame[
        [
            column
            for column in [
                "id",
                *STANDARD_LEAD_EXPORT_COLUMNS,
                "lead_reason",
                "agency_kit",
                "offer_match",
                "whatsapp_sales_kit",
                "marketing_campaign_kit",
            ]
            if column in frame.columns
        ]
    ]


@st.cache_data(ttl=30, show_spinner=False)
def load_dashboard_snapshot_cached() -> Dict[str, Any]:
    response = api_request("GET", "/dashboard/snapshot")
    snapshot = parse_api_json(response, "DASHBOARD_SNAPSHOT_FAILED")
    if not response.is_success:
        raise RuntimeError(str(snapshot.get("detail", "Could not load dashboard snapshot.")))
    return snapshot


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
        return {"status": "FAILED", "reason": fallback_reason, "detail": raw_text[:500] or fallback_reason}


def enqueue_job(agent_name: str, payload: Dict[str, Any] | None = None, *, run_now: bool = True) -> Dict[str, Any]:
    response = api_request("POST", "/jobs", json={"agent_name": agent_name, "payload": payload or {}})
    body = parse_api_json(response)
    if not response.is_success:
        raise RuntimeError(str(body.get("detail", f"Could not enqueue {agent_name}.")))
    try:
        load_recent_jobs.clear()
        load_dashboard_snapshot_cached.clear()
    except Exception:
        pass
    result: Dict[str, Any] = {"queued": body}
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
            return result
        run_body = parse_api_json(run_response)
        result["run"] = run_body
        if not run_response.is_success:
            st.error(str(run_body.get("detail", f"Could not run {agent_name}.")))
            return result
        status = str(run_body.get("status", "")).strip().lower()
        agent_status = str(run_body.get("agent_status", "")).strip().upper()
        if status == "failed" or agent_status == "FAILED":
            st.error(str(run_body.get("message", f"Could not run {agent_name}.")))
            return result
    return result


def start_lead_generation_job(payload: Dict[str, Any]) -> Dict[str, Any]:
    result = enqueue_job("lead_generation", payload, run_now=False)
    queued = dict(result.get("queued", {}) or {})
    job_id = str(queued.get("job_id", "") or "").strip()
    if job_id:
        st.session_state["active_lead_generation_job_id"] = job_id
    return result


def load_job_status(job_id: str) -> Dict[str, Any] | None:
    if not job_id:
        return {}
    try:
        response = api_request("GET", f"/jobs/{job_id}/status", timeout=12)
        body = parse_api_json(response)
    except (httpx.RequestError, httpx.TimeoutException, httpx.HTTPError):
        return None
    except Exception:
        return None
    return body if response.is_success else None


def load_job_events(job_id: str) -> List[Dict[str, Any]]:
    if not job_id:
        return []
    response = api_request("GET", f"/jobs/{job_id}/events")
    body = parse_api_json(response)
    if not response.is_success:
        return []
    return list(body.get("events", []) or [])


def parse_datetime_value(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def is_active_lead_generation_job(job: Dict[str, Any], now: datetime | None = None) -> bool:
    if str(job.get("job_type") or job.get("name") or "").strip() != "lead_generation":
        return False
    if str(job.get("status", "") or "").strip().lower() not in {"queued", "running"}:
        return False
    updated_at = parse_datetime_value(job.get("updated_at") or job.get("created_at"))
    if updated_at is None:
        return True
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc) - updated_at <= timedelta(minutes=30)


def render_active_lead_generation_job() -> bool:
    job_id = str(st.session_state.get("active_lead_generation_job_id", "") or "").strip()
    if not job_id:
        return False
    status = load_job_status(job_id)
    if status is None:
        st.warning("Could not refresh job status right now.")
        st.info("No active jobs.")
        st.session_state["active_lead_generation_job_id"] = ""
        st.session_state["lead_generation_busy"] = False
        return False
    if not status:
        st.session_state["active_lead_generation_job_id"] = ""
        st.session_state["lead_generation_busy"] = False
        return False

    normalized_status = str(status.get("status", "") or "").strip().lower()
    active = is_active_lead_generation_job(
        {
            "job_type": "lead_generation",
            "status": normalized_status,
            "updated_at": status.get("updated_at") or status.get("created_at"),
        }
    )
    progress = int(status.get("progress_percentage", 0) or 0)
    current_stage = str(status.get("current_stage", "") or normalized_status or "queued").strip()
    st.markdown('<div class="page-section page-card"><div class="page-card-inner">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Lead Generation Progress</div>', unsafe_allow_html=True)
    st.caption(f"Job: {job_id} | Status: {normalized_status.title() or 'Queued'} | Stage: {current_stage.title()} | Progress: {progress}%")
    st.progress(max(0, min(100, progress)))

    events = load_job_events(job_id)
    if events:
        rows = []
        for event in list(events)[-8:]:
            rows.append(
                {
                    "stage": str(event.get("stage", "")),
                    "status": str(event.get("status", "")),
                    "domain": str(event.get("domain", "")),
                    "message": str(event.get("message", "")),
                    "reason": str(event.get("reason", "")),
                }
            )
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    if normalized_status == "completed":
        render_lead_generation_result({"data": dict(status.get("result", {}) or {}), "message": str(status.get("message", "") or "")})
        st.session_state["active_lead_generation_job_id"] = ""
        st.session_state["lead_generation_busy"] = False
        st.rerun()
    elif normalized_status == "failed":
        st.error(str(status.get("message") or status.get("error") or "Lead generation failed safely."))
        st.session_state["active_lead_generation_job_id"] = ""
        st.session_state["lead_generation_busy"] = False
    elif normalized_status in {"queued", "running"} and not active:
        st.info("No active jobs.")
        st.session_state["active_lead_generation_job_id"] = ""
        st.session_state["lead_generation_busy"] = False
    elif active:
        st.info("Lead generation is running in the background. You can keep using the app while it works.")
        st.session_state["lead_generation_busy"] = True
    st.markdown("</div></div>", unsafe_allow_html=True)
    return active


def render_lead_generation_result(run_body: Dict[str, Any]) -> None:
    data = dict(run_body.get("data", {}) or {}) if isinstance(run_body.get("data", {}), dict) else {}
    saved = int(data.get("saved_leads", data.get("lead_count", 0)) or 0)
    if saved > 0:
        st.success(f"Lead generation completed. Saved {saved} lead(s).")
        return
    if not data:
        return
    st.warning(str(run_body.get("message") or data.get("message") or "Lead generation finished with 0 saved leads."))
    st.caption(
        "Raw results: "
        f"{int(data.get('raw_results_count', 0) or 0)} | "
        "Filtered results: "
        f"{int(data.get('filtered_results_count', 0) or 0)} | "
        "URLs found: "
        f"{int(data.get('discovered_urls_count', 0) or 0)} | "
        f"Pages scraped: {int(data.get('scraped_pages_count', 0) or 0)} | "
        f"Emails extracted: {int(data.get('extracted_emails_count', 0) or 0)} | "
        f"Leads saved: {saved}"
    )
    rejected_sources = int(data.get("rejected_bad_domain_count", 0) or 0) + int(data.get("rejected_irrelevant_count", 0) or 0)
    if rejected_sources:
        st.info(
            f"Only {saved} usable leads found. Reason: many search results were rejected as irrelevant, directories, "
            "jobs, GitHub, government, or embassy pages. Try a more specific query like software houses in Lahore."
        )
    reasons = data.get("rejection_reasons", {})
    if isinstance(reasons, dict) and reasons:
        reason_text = ", ".join(f"{key}: {value}" for key, value in list(reasons.items())[:5])
        st.caption(f"Rejection reasons: {reason_text}")


def load_outreach_preflight() -> Dict[str, Any]:
    response = api_request("GET", "/outreach/preflight")
    body = parse_api_json(response)
    if not response.is_success:
        return {
            "gmail_connected": False,
            "gmail_status": "missing_credentials",
            "gmail_status_label": "Missing credentials",
            "gmail_error": "gmail_not_connected",
            "last_successful_send": "",
            "pending_sendable_count": 0,
            "retryable_failed_count": 0,
            "gmail_api_disabled_blocked_count": 0,
            "sendable_count": 0,
            "already_sent_count": 0,
            "no_email_count": 0,
            "failed_without_reason_count": 0,
            "sample_errors": [],
        }
    return body


def load_system_health() -> Dict[str, Any]:
    response = api_request("GET", "/system/health")
    body = parse_api_json(response)
    if not response.is_success:
        return {"status": "fail", "checks": {"system": {"status": "fail", "message": str(body.get("detail", "Health check failed."))}}}
    return body


def load_lead_quality_export() -> pd.DataFrame:
    response = api_request("GET", "/leads/export/quality")
    body = parse_api_json(response)
    if not response.is_success:
        st.error(str(body.get("detail", "Could not load lead quality export.")))
        return pd.DataFrame()
    return pd.DataFrame(body.get("items", []))


def render_outreach_preflight_summary(preflight: Dict[str, Any]) -> None:
    pending_sendable_count = int(preflight.get("pending_sendable_count", 0) or 0)
    retryable_failed_count = int(preflight.get("retryable_failed_count", 0) or 0)
    sendable_count = int(preflight.get("sendable_count", 0) or 0)
    already_sent_count = int(preflight.get("already_sent_count", 0) or 0)
    no_email_count = int(preflight.get("no_email_count", 0) or 0)
    api_disabled_blocked_count = int(preflight.get("gmail_api_disabled_blocked_count", 0) or 0)
    gmail_connected = bool(preflight.get("gmail_connected"))
    gmail_status_label = str(preflight.get("gmail_status_label", "") or ("Connected" if gmail_connected else "Missing credentials"))
    last_successful_send = str(preflight.get("last_successful_send", "") or "")
    st.caption(
        "Sendable email leads: "
        f"{sendable_count} | "
        f"Pending: {pending_sendable_count} | "
        f"Retryable failed: {retryable_failed_count} | "
        f"Gmail API blocked: {api_disabled_blocked_count} | "
        f"Already sent: {already_sent_count} | "
        f"Leads without verified email: {no_email_count} | "
        f"Gmail: {gmail_status_label}"
    )
    if last_successful_send:
        st.caption(f"Last successful send: {last_successful_send}")
    if pending_sendable_count == 0 and retryable_failed_count > 0:
        st.info(
            f"No new pending email leads found, but {retryable_failed_count} failed verified-email leads can be retried. "
            "Send Outreach will include retryable failed leads automatically."
        )


def render_add_test_lead_form() -> None:
    if not can_show_test_lead_tool():
        return
    with st.expander("Add test lead", expanded=False):
        st.caption("Admin/development test utility for safe email outreach checks. Use a verified email on the same domain as the company URL.")
        with st.form("manual_test_lead_form"):
            cols = st.columns(2)
            with cols[0]:
                company_url = st.text_input("Company URL", value="", placeholder="https://example.com")
                industry = st.text_input("Industry", value="", placeholder="e.g. software")
            with cols[1]:
                verified_email = st.text_input("Verified email", value="", placeholder="you@example.com")
                country = st.text_input("Country", value="", placeholder="e.g. Pakistan")
            submitted = st.form_submit_button("Add test lead", use_container_width=True)
        if submitted:
            clean_url = company_url.strip()
            clean_email = verified_email.strip().lower()
            if not clean_url or not clean_email:
                st.error("Company URL and verified email are required for a test email lead.")
                return
            if "@" not in clean_email or "." not in clean_email.rsplit("@", 1)[-1]:
                st.error("Enter a valid verified email before testing outreach.")
                return
            response = api_request(
                "POST",
                "/leads",
                json={
                    "company_url": clean_url,
                    "website": clean_url,
                    "verified_email": clean_email,
                    "email": clean_email,
                    "industry": industry.strip(),
                    "country": country.strip(),
                    "status": "pending",
                    "outreach_status": "pending",
                    "service_reason": "Manual test lead for verified email outreach.",
                    "metadata": {"source": "manual_test_lead", "created_from": "dashboard_test_lead_tool"},
                },
            )
            payload = parse_api_json(response)
            if response.is_success:
                st.success("Test lead added with verified_email and pending outreach status.")
                st.rerun()
            else:
                st.error(str(payload.get("detail", "Could not add test lead.")))


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
    search_preview = build_dashboard_search_query(target_industry, target_country)
    if search_preview:
        st.caption(f"Search query: {search_preview}")

    active_generation = render_active_lead_generation_job()
    busy = active_generation or bool(st.session_state.get("lead_generation_busy", False))
    st.caption("This may take 1-3 minutes. You can keep the page open while the job runs.")
    if st.button("Generate Leads", use_container_width=True, disabled=busy):
        search_query = build_dashboard_search_query(target_industry, target_country)

        st.session_state["lead_generation_busy"] = True
        try:
            job_result = start_lead_generation_job(
                {
                    "limit": 10,
                    "query": search_query,
                    "niche": target_industry.strip(),
                    "country": target_country.strip(),
                    "location": target_country.strip(),
                }
            )
            queued = dict(job_result.get("queued", {}) or {})
            if queued.get("existing"):
                st.info("Lead generation is already running. Showing the active job.")
            else:
                st.success("Lead generation started.")
            st.rerun()
        except Exception as error:
            st.session_state["lead_generation_busy"] = False
            st.error(str(error))

    outreach_preflight: Dict[str, Any] = {}
    if pro_enabled:
        try:
            outreach_preflight = load_outreach_preflight()
            render_outreach_preflight_summary(outreach_preflight)
        except Exception:
            outreach_preflight = {}

    render_add_test_lead_form()

    action_cols = st.columns(3)
    with action_cols[0]:
        if st.button("Send Outreach - Pro", use_container_width=True, disabled=not pro_enabled):
            if not outreach_preflight:
                outreach_preflight = load_outreach_preflight()
            if int(outreach_preflight.get("sendable_count", 0) or 0) <= 0:
                st.warning(NO_VERIFIED_EMAIL_LEADS_MESSAGE)
                st.markdown("</div></div>", unsafe_allow_html=True)
                return
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
                '<span class="premium-badge">Locked - Pro</span>'
                f'<h4>{html.escape(label)}</h4>'
                f'<p>{html.escape(LOCKED_FEATURE_MESSAGE)}</p>'
                "</div>"
            )
            for label in locked_features
        )
        st.markdown(f'<div class="locked-grid">{locked_cards}</div>', unsafe_allow_html=True)
    st.markdown("</div></div>", unsafe_allow_html=True)


@st.cache_data(ttl=30, show_spinner=False)
def load_recent_jobs(limit: int = 10) -> List[Dict[str, Any]]:
    try:
        response = api_request("GET", "/jobs/recent")
        payload = parse_api_json(response)
        if response.is_success:
            return list(payload.get("items", []) or [])[: max(1, int(limit or 10))]
    except Exception:
        return []
    return []


def render_lead_pipeline_monitor(latest_job: Dict[str, Any]) -> None:
    if str(latest_job.get("job_type", "")).strip() != "lead_generation":
        return
    summary = latest_job.get("result_summary", {})
    if not isinstance(summary, dict):
        return
    stats = summary.get("stats", {})
    events = list(summary.get("events", []) or [])[-30:]
    st.markdown('<div class="page-section page-card"><div class="page-card-inner">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Lead Pipeline Monitor</div>', unsafe_allow_html=True)
    st.caption(
        f"Stage: {str(summary.get('current_stage', latest_job.get('status', ''))).title()} | "
        f"Progress: {int(summary.get('progress_percentage', 0) or 0)}%"
    )
    cols = st.columns(6)
    cols[0].metric("Raw Results", int(stats.get("raw_results_count", 0) or 0) if isinstance(stats, dict) else 0)
    cols[1].metric("Valid Domains", int(stats.get("filtered_results_count", 0) or 0) if isinstance(stats, dict) else 0)
    cols[2].metric("Saved Leads", int(stats.get("saved_leads", 0) or 0) if isinstance(stats, dict) else 0)
    cols[3].metric("Rejected", int(stats.get("rejected_leads_count", 0) or 0) if isinstance(stats, dict) else 0)
    cols[4].metric("A-Grade", int(stats.get("a_grade_leads", 0) or 0) if isinstance(stats, dict) else 0)
    cols[5].metric("Outreach Ready", int(stats.get("outreach_ready_leads", 0) or 0) if isinstance(stats, dict) else 0)
    if events:
        rows = []
        for event in reversed(events):
            rows.append(
                {
                    "stage": str(event.get("stage", "")),
                    "status": str(event.get("status", "")),
                    "domain": str(event.get("domain", "")),
                    "reason": str(event.get("reason", "")),
                    "message": str(event.get("message", "")),
                }
            )
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    st.markdown("</div></div>", unsafe_allow_html=True)


def render_generation_status_card(snapshot: Dict[str, Any]) -> None:
    jobs = load_recent_jobs()
    latest_job = jobs[0] if jobs else {}
    queued_or_running = [job for job in jobs if is_active_lead_generation_job(job)]
    metrics = [
        ("Last job status", str(latest_job.get("status", "unknown") or "unknown").title(), "Current pipeline"),
        ("Total leads", str(int(snapshot.get("lead_count", 0) or 0)), "Saved in workspace"),
        ("Jobs queued/running", str(len(queued_or_running)), "Active processing"),
        ("Last refresh", datetime.utcnow().strftime("%H:%M:%S UTC"), "Latest sync"),
    ]
    cards = "".join(render_metric_card(label, value, hint) for label, value, hint in metrics)
    st.markdown(f'<div class="page-section"><div class="metric-card-grid">{cards}</div></div>', unsafe_allow_html=True)
    if queued_or_running:
        render_lead_pipeline_monitor(queued_or_running[0])
    else:
        st.caption("No active jobs.")
    with st.expander("Recent jobs", expanded=False):
        if jobs:
            st.dataframe(pd.DataFrame(jobs), use_container_width=True, hide_index=True)
        else:
            st.caption("No recent jobs.")
    if str(latest_job.get("job_type", "")).strip() == "lead_generation":
        result = latest_job.get("result", {})
        if isinstance(result, dict) and int(result.get("saved_leads", result.get("lead_count", 0)) or 0) == 0:
            message = str(latest_job.get("message") or result.get("message") or "").strip()
            if message:
                st.warning(message)
            st.caption(
                "Last lead-generation result: "
                f"raw results {int(result.get('raw_results_count', 0) or 0)}, "
                f"filtered results {int(result.get('filtered_results_count', 0) or 0)}, "
                f"URLs found {int(result.get('discovered_urls_count', 0) or 0)}, "
                f"pages scraped {int(result.get('scraped_pages_count', 0) or 0)}, "
                f"emails extracted {int(result.get('extracted_emails_count', 0) or 0)}, "
                f"leads saved {int(result.get('saved_leads', 0) or 0)}."
            )
    contact_metrics = [
        ("Email-ready Leads", str(int(snapshot.get("email_ready_leads", 0) or 0)), "Verified business email"),
        ("Phone-only Leads", str(int(snapshot.get("phone_only_leads", 0) or 0)), "WhatsApp or call first"),
        ("No-contact Leads", str(int(snapshot.get("no_contact_leads", 0) or 0)), "Needs enrichment"),
        ("Verified email rate", f"{float(snapshot.get('verified_email_rate', 0) or 0):.1f}%", "Ready for outreach"),
    ]
    contact_cards = "".join(render_metric_card(label, value, hint) for label, value, hint in contact_metrics)
    st.markdown(f'<div class="page-section"><div class="metric-card-grid">{contact_cards}</div></div>', unsafe_allow_html=True)


def render_dashboard_page(frame: pd.DataFrame, snapshot: Dict[str, Any]) -> None:
    st.markdown('<div class="page-section page-card"><div class="page-card-inner">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Pipeline Overview</div>', unsafe_allow_html=True)
    st.markdown('<div class="section-caption">A compact view of replies, bounces, and scheduled follow-ups.</div>', unsafe_allow_html=True)
    metrics = st.columns(3)
    metrics[0].metric("Replies", int(snapshot.get("reply_count", 0)))
    metrics[1].metric("Bounced", int(snapshot.get("bounced_count", 0)))
    metrics[2].metric("Scheduled Follow-ups", int(snapshot.get("scheduled_followup_count", 0)))
    if frame.empty:
        render_empty_state("No leads yet", "Enter a niche and country to generate your first leads.", "Generate Leads")
        st.markdown("</div></div>", unsafe_allow_html=True)
        return
    chart_left, chart_right = st.columns(2)
    with chart_left:
        st.plotly_chart(px.histogram(frame, x="score", nbins=10, title="Lead Scores"), use_container_width=True)
    with chart_right:
        st.plotly_chart(px.pie(frame, names="outreach_status", title="Pipeline"), use_container_width=True)
    st.markdown("</div></div>", unsafe_allow_html=True)


def render_table_page(frame: pd.DataFrame, columns: List[str], *, show_csv_export: bool = True) -> None:
    st.markdown('<div class="page-section page-card"><div class="page-card-inner dashboard-actions">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Lead Table</div>', unsafe_allow_html=True)
    st.markdown('<div class="section-caption">Review saved leads and export the visible columns.</div>', unsafe_allow_html=True)
    original_count = len(frame)
    visible = apply_lead_table_controls(frame.copy())
    for column in columns:
        if column not in visible.columns:
            visible[column] = ""
    export_frame = visible[columns]
    if export_frame.empty:
        render_empty_state("No leads yet", "Enter a niche and country to generate your first leads.", "Generate Leads")
    else:
        st.caption(f"Showing {len(export_frame)} of {original_count} leads.")
        st.dataframe(export_frame, use_container_width=True, hide_index=True)
        if show_csv_export:
            csv_frame = sanitize_csv_frame(export_frame)
            st.download_button(
                "Export Visible Leads CSV",
                csv_frame.to_csv(index=False).encode("utf-8"),
                file_name="leads.csv",
                mime="text/csv",
                use_container_width=True,
            )
    st.markdown("</div></div>", unsafe_allow_html=True)


def truncate_text(value: str, limit: int = 120) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 3)].rstrip()}..."


def whatsapp_phone_badge(phone: str) -> str:
    value = str(phone or "").strip()
    if not value:
        return "⚠️ Missing"
    digits = "".join(char for char in value if char.isdigit())
    if 8 <= len(digits) <= 15 and len(set(digits)) > 2:
        return "✅ Valid"
    return "❌ Invalid"


def whatsapp_phone_is_valid(phone: str) -> bool:
    return whatsapp_phone_badge(phone).startswith("✅")


def whatsapp_link_for_phone(phone: str, message: str = "") -> str:
    digits = "".join(char for char in str(phone or "") if char.isdigit())
    if not whatsapp_phone_is_valid(phone):
        return ""
    text = str(message or "").strip()
    if not text:
        return f"https://wa.me/{digits}"
    from urllib.parse import quote

    return f"https://wa.me/{digits}?text={quote(text)}"


def sort_whatsapp_frame(frame: pd.DataFrame, sort_by: str) -> pd.DataFrame:
    if sort_by == "Highest Score" and "score" in frame.columns:
        return frame.assign(_score=pd.to_numeric(frame["score"], errors="coerce").fillna(0)).sort_values("_score", ascending=False).drop(columns=["_score"])
    if sort_by in {"Newest", "Oldest"} and "created_at" in frame.columns:
        created = pd.to_datetime(frame["created_at"], errors="coerce", utc=True)
        return frame.assign(_created_at=created).sort_values("_created_at", ascending=sort_by == "Oldest").drop(columns=["_created_at"])
    if sort_by == "Oldest":
        return frame.iloc[::-1]
    return frame


def apply_lead_table_controls(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    with st.expander("Filter and sort leads", expanded=True):
        if st.button("Reset filters", use_container_width=False):
            for key in list(st.session_state.keys()):
                if str(key).startswith("lead_filter_"):
                    del st.session_state[key]
            st.rerun()
        c1, c2, c3 = st.columns(3)
        with c1:
            search = st.text_input("Search company/domain/email/phone", key="lead_filter_search")
        with c2:
            sort_by = st.selectbox("Sort by", ["Newest first", "Oldest first", "Highest score"], key="lead_filter_sort")
        with c3:
            readiness = st.radio("Quick toggle", ["All", "Email-ready", "Phone-ready"], horizontal=True, key="lead_filter_readiness")

    filtered = frame.copy()
    if search:
        haystack = (
            filtered.get("company", pd.Series("", index=filtered.index)).astype(str)
            + " "
            + filtered.get("company_url", pd.Series("", index=filtered.index)).astype(str)
            + " "
            + filtered.get("verified_email", pd.Series("", index=filtered.index)).astype(str)
            + " "
            + filtered.get("phone", pd.Series("", index=filtered.index)).astype(str)
        ).str.lower()
        filtered = filtered[haystack.str.contains(str(search).lower(), na=False)]
    if readiness == "Email-ready" and "verified_email" in filtered.columns:
        filtered = filtered[filtered["verified_email"].astype(str).str.strip().ne("")]
    if readiness == "Phone-ready" and "phone" in filtered.columns:
        filtered = filtered[filtered["phone"].astype(str).str.strip().ne("")]
    filtered = sort_lead_frame(filtered, sort_by)
    if filtered.empty:
        st.info("No leads match these filters.")
    return filtered


def sort_lead_frame(frame: pd.DataFrame, sort_by: str) -> pd.DataFrame:
    if frame.empty:
        return frame
    if sort_by in {"Highest score first", "Highest score"} and "score" in frame.columns:
        return frame.assign(_score=pd.to_numeric(frame["score"], errors="coerce").fillna(0)).sort_values("_score", ascending=False).drop(columns=["_score"])
    if sort_by == "Lowest score first" and "score" in frame.columns:
        return frame.assign(_score=pd.to_numeric(frame["score"], errors="coerce").fillna(0)).sort_values("_score", ascending=True).drop(columns=["_score"])
    if sort_by == "A-grade first" and "lead_quality_grade" in frame.columns:
        order = {"A": 0, "B": 1, "C": 2, "Rejected": 3}
        return frame.assign(_grade=frame["lead_quality_grade"].map(order).fillna(9)).sort_values("_grade").drop(columns=["_grade"])
    if sort_by == "Outreach ready first" and "verified_email" in frame.columns:
        return frame.assign(_ready=frame["verified_email"].astype(str).str.len() > 0).sort_values("_ready", ascending=False).drop(columns=["_ready"])
    if sort_by == "Email available first" and "verified_email" in frame.columns:
        return frame.assign(_email=frame["verified_email"].astype(str).str.len() > 0).sort_values("_email", ascending=False).drop(columns=["_email"])
    if sort_by == "Company name A-Z" and "company" in frame.columns:
        return frame.sort_values("company")
    if sort_by in {"Newest first", "Oldest first"} and "created_at" in frame.columns:
        created = pd.to_datetime(frame["created_at"], errors="coerce", utc=True)
        return frame.assign(_created_at=created).sort_values("_created_at", ascending=sort_by == "Oldest first").drop(columns=["_created_at"])
    if sort_by == "Oldest first":
        return frame.iloc[::-1]
    return frame


def render_lead_action_cta(row: pd.Series) -> None:
    readiness = str(row.get("contact_readiness", "") or "").strip()
    outreach_status = str(row.get("outreach_status", "") or "pending").strip().replace("_", " ").title()
    if readiness == "Phone-only":
        st.info("Phone-only lead: generate a WhatsApp Sales Kit for the first touch.")
    elif readiness in {"Email-ready", "Likely email"}:
        st.success(f"Email-ready lead. Outreach status: {outreach_status or 'Pending'}")
    else:
        st.warning("No contact found yet. Enrich this lead before outreach.")


def render_whatsapp_crm_row(row: pd.Series, index: int) -> None:
    lead_id = str(row.get("id", "") or "").strip()
    if not lead_id:
        return
    company = str(row.get("company", "") or row.get("company_url", "") or "Lead").strip()
    website = str(row.get("company_url", "") or "").strip()
    lead_reason = str(row.get("lead_reason", "") or row.get("service_reason", "") or "").strip()
    phone = str(row.get("phone", "") or "").strip()
    valid = whatsapp_phone_is_valid(phone)
    status_raw = str(row.get("whatsapp_status", "not_contacted") or "not_contacted").strip().lower()
    status = status_raw.replace("_", " ").title()
    score = int(row.get("score", 0) or 0)
    preview_key = f"whatsapp_preview_{lead_id}"
    message = ""
    whatsapp_url = ""
    cached = st.session_state.get(preview_key, {}) if isinstance(st.session_state.get(preview_key, {}), dict) else {}
    if cached:
        message = str(cached.get("whatsapp_message", "") or "").strip()
        whatsapp_url = str(cached.get("whatsapp_url", "") or "").strip()
    if valid:
        whatsapp_url = whatsapp_url or whatsapp_link_for_phone(phone, message)
    st.markdown('<div class="page-section page-card"><div class="page-card-inner">', unsafe_allow_html=True)
    cols = st.columns([1.3, 1.4, 1, 0.9, 0.6, 1.7, 1, 1.9])
    cols[0].write(company or "Lead")
    cols[1].write(website or "-")
    cols[2].write(phone or "-")
    cols[3].write(whatsapp_phone_badge(phone))
    cols[4].write(score)
    cols[5].write(truncate_text(lead_reason, 120) or "-")
    cols[6].write(status)
    with cols[7]:
        if st.button("Generate Message", key=f"generate_whatsapp_{lead_id}_{index}", use_container_width=True):
            preview_response = api_request("POST", f"/leads/{lead_id}/whatsapp-message/preview", json={})
            preview = parse_api_json(preview_response, "WHATSAPP_PREVIEW_FAILED")
            if preview_response.is_success:
                st.session_state[preview_key] = preview
                message = str(preview.get("whatsapp_message", "") or "").strip()
                whatsapp_url = str(preview.get("whatsapp_url", "") or "").strip() or whatsapp_link_for_phone(phone, message)
            else:
                st.warning(str(preview.get("detail", "Could not generate WhatsApp preview.")))
    if cached or message:
        st.text_area("Message preview", value=message, height=96, key=f"whatsapp_message_preview_{lead_id}_{index}")
    action_cols = st.columns(6)
    with action_cols[0]:
        st.text_area("Copy Message", value=message, height=80, key=f"copy_whatsapp_{lead_id}_{index}", disabled=not bool(message))
    with action_cols[1]:
        if valid and whatsapp_url:
            if st.button("Open WhatsApp", key=f"open_whatsapp_{lead_id}_{index}", use_container_width=True):
                api_request("POST", f"/leads/{lead_id}/whatsapp-status", json={"whatsapp_status": "opened", "whatsapp_message": message})
                st.link_button("Continue", whatsapp_url, use_container_width=True)
        else:
            st.button("Open WhatsApp", key=f"open_whatsapp_disabled_{lead_id}_{index}", disabled=True, use_container_width=True)
            st.caption("Invalid or missing phone number.")
    for label, value, col in [
        ("Mark Contacted", "contacted", action_cols[2]),
        ("Mark Replied", "replied", action_cols[3]),
        ("Mark Interested", "interested", action_cols[4]),
        ("Mark Not Interested", "not_interested", action_cols[5]),
    ]:
        with col:
            if st.button(label, key=f"whatsapp_status_{value}_{lead_id}_{index}", use_container_width=True):
                response = api_request("POST", f"/leads/{lead_id}/whatsapp-status", json={"whatsapp_status": value, "whatsapp_message": message})
                payload = parse_api_json(response, "WHATSAPP_STATUS_FAILED")
                if response.is_success:
                    st.success("WhatsApp status updated.")
                    st.rerun()
                else:
                    st.error(str(payload.get("detail", "Could not update WhatsApp status.")))
    st.markdown("</div></div>", unsafe_allow_html=True)


def render_whatsapp_crm_page(tenant: TenantContext) -> None:
    render_module_header("WhatsApp CRM", "Manage phone-ready leads and open WhatsApp manually. No auto-sending.")
    frame = load_dashboard_data(tenant)
    st.markdown('<div class="page-section page-card"><div class="page-card-inner dashboard-actions">', unsafe_allow_html=True)
    if frame.empty:
        render_empty_state("No phone-ready leads yet", "Generate leads with phone numbers first.", "Generate Leads")
        st.markdown("</div></div>", unsafe_allow_html=True)
        return
    frame = frame.copy()
    frame["phone_badge"] = frame.get("phone", pd.Series("", index=frame.index)).astype(str).map(whatsapp_phone_badge)
    frame["phone_valid"] = frame["phone_badge"].str.startswith("✅")
    frame = frame[frame["phone_valid"]].copy()
    total_phone = int(frame.get("phone", pd.Series("", index=frame.index)).astype(str).str.strip().ne("").sum())
    valid_count = int(frame["phone_valid"].sum())
    invalid_count = int(len(frame) - valid_count)
    contacted_count = int(frame.get("whatsapp_status", pd.Series("", index=frame.index)).astype(str).str.lower().isin(["contacted", "opened"]).sum())
    replied_interested_count = int(frame.get("whatsapp_status", pd.Series("", index=frame.index)).astype(str).str.lower().isin(["replied", "interested"]).sum())
    cards = [
        ("Total phone leads", total_phone, "Rows with any phone value"),
        ("Valid numbers", valid_count, "Ready for wa.me"),
        ("Invalid/missing numbers", invalid_count, "Needs cleanup"),
        ("Contacted", contacted_count, "Opened or contacted"),
        ("Replied / Interested", replied_interested_count, "Positive conversation state"),
    ]
    st.markdown('<div class="metric-card-grid">' + "".join(render_metric_card(label, value, hint) for label, value, hint in cards) + "</div>", unsafe_allow_html=True)
    filter_cols = st.columns([1.6, 1, 1.2, 1])
    with filter_cols[0]:
        search = st.text_input("Search company/domain/phone", key="whatsapp_crm_search")
    with filter_cols[1]:
        sort_by = st.selectbox("Sort", ["Newest", "Oldest", "Highest Score"], key="whatsapp_crm_sort")
    with filter_cols[2]:
        status_filter = st.selectbox("Status", ["All", "Not Contacted", "Contacted", "Replied", "Interested", "Not Interested"], key="whatsapp_crm_status")
    with filter_cols[3]:
        phone_filter = st.selectbox("Phone", ["All", "Valid only", "Invalid only"], key="whatsapp_crm_phone")
    filtered = frame.copy()
    if search:
        haystack = (
            filtered.get("company", pd.Series("", index=filtered.index)).astype(str)
            + " "
            + filtered.get("company_url", pd.Series("", index=filtered.index)).astype(str)
            + " "
            + filtered.get("phone", pd.Series("", index=filtered.index)).astype(str)
        ).str.lower()
        filtered = filtered[haystack.str.contains(str(search).lower(), na=False)]
    if status_filter != "All":
        wanted = status_filter.lower().replace(" ", "_")
        filtered = filtered[filtered.get("whatsapp_status", pd.Series("", index=filtered.index)).astype(str).str.lower().eq(wanted)]
    if phone_filter == "Valid only":
        filtered = filtered[filtered["phone_valid"]]
    if phone_filter == "Invalid only":
        filtered = filtered[~filtered["phone_valid"]]
    filtered = sort_whatsapp_frame(filtered, sort_by)
    if filtered.empty:
        render_empty_state("No phone-ready leads yet", "Generate leads with phone numbers first.", "Generate Leads")
        st.markdown("</div></div>", unsafe_allow_html=True)
        return
    header_cols = st.columns([1.3, 1.4, 1, 0.9, 0.6, 1.7, 1, 1.9])
    for col, label in zip(header_cols, ["Company", "Website / Domain", "Phone", "Number Valid?", "Score", "Lead Reason", "WhatsApp Status", "Actions"]):
        col.markdown(f"**{label}**")
    st.markdown("</div></div>", unsafe_allow_html=True)
    for index, row in filtered.head(100).iterrows():
        render_whatsapp_crm_row(row, int(index) if isinstance(index, int) else 0)


@st.cache_data(ttl=20, show_spinner=False)
def load_voice_calls(_tenant: TenantContext | None = None, limit: int = 50) -> List[Dict[str, Any]]:
    response = api_request("GET", f"/voice/calls?limit={max(1, min(100, int(limit or 50)))}")
    payload = safe_voice_api_json(response)
    if payload is None:
        st.warning("Voice Calls API is not available right now.")
        return []
    if not response.is_success:
        st.warning(str(payload.get("detail", "Voice Calls API is not available right now.")))
        return []
    items = payload.get("items", [])
    return list(items) if isinstance(items, list) else []


def safe_voice_api_json(response: httpx.Response) -> Dict[str, Any] | None:
    status_code = int(getattr(response, "status_code", 0) or 0)
    headers = getattr(response, "headers", {}) or {}
    content_type = str(headers.get("content-type", "") if hasattr(headers, "get") else "").lower()
    if "application/json" not in content_type:
        safe_text = str(getattr(response, "text", "") or "")[:160]
        print(f"Voice Calls API unavailable status={status_code} body={safe_text!r}")
        return None
    try:
        payload = response.json()
    except ValueError:
        safe_text = str(getattr(response, "text", "") or "")[:160]
        print(f"Voice Calls API invalid JSON status={status_code} body={safe_text!r}")
        return None
    return payload if isinstance(payload, dict) else None


def render_voice_calls_page(tenant: TenantContext) -> None:
    render_page_header("Voice Calls", "Review Vapi call outcomes and start small, safe voice campaigns.", "Voice")
    try:
        status_response = api_request("GET", "/voice/agent/status", timeout=8)
        status_payload = status_response.json()
        if not status_response.is_success:
            raise RuntimeError(str(status_payload.get("detail", "Voice provider status unavailable.")))
    except Exception as error:
        status_payload = {}
        st.warning(f"Voice provider status unavailable: {error}")

    st.markdown(
        '<div class="metric-grid">'
        + render_metric_card("Configured", "Yes" if status_payload.get("configured") else "No")
        + render_metric_card("Provider Reachable", "Yes" if status_payload.get("provider_reachable") else "No")
        + "</div>",
        unsafe_allow_html=True,
    )

    calls = load_voice_calls(tenant, 50)

    metrics = [
        render_metric_card("Total Calls", len(calls)),
        render_metric_card("Interested", sum(1 for item in calls if item.get("outcome") == "interested")),
        render_metric_card("Callbacks", sum(1 for item in calls if item.get("outcome") == "callback")),
        render_metric_card("No Answer", sum(1 for item in calls if item.get("outcome") == "no_answer")),
        render_metric_card("Failed", sum(1 for item in calls if item.get("status") == "failed")),
    ]
    st.markdown('<div class="metric-grid">' + "".join(metrics) + "</div>", unsafe_allow_html=True)

    c1, c2, c3 = st.columns(3)
    with c1:
        status_filter = st.selectbox("Status", ["All", "active", "completed", "failed", "pending"], key="voice_status_filter")
    with c2:
        outcome_filter = st.selectbox("Outcome", ["All", "interested", "callback", "not_interested", "no_answer", "voicemail", "unknown"], key="voice_outcome_filter")
    with c3:
        sort_order = st.selectbox("Sort", ["Newest first", "Oldest first"], key="voice_sort")

    filtered_calls = list(calls)
    if status_filter != "All":
        filtered_calls = [item for item in filtered_calls if item.get("status") == status_filter]
    if outcome_filter != "All":
        filtered_calls = [item for item in filtered_calls if item.get("outcome") == outcome_filter]
    filtered_calls = sorted(filtered_calls, key=lambda item: str(item.get("created_at", "")), reverse=sort_order == "Newest first")
    visible_calls = filtered_calls[:20]
    if visible_calls:
        st.dataframe(
            pd.DataFrame(visible_calls)[["id", "lead_id", "status", "outcome", "summary", "duration_seconds", "called_at"]],
            use_container_width=True,
            hide_index=True,
        )
    else:
        render_empty_state("No voice calls yet", "Start a small voice campaign from leads with phone numbers.")

    call_labels = {str(item.get("id", "")): f"{item.get('status', '')} | {item.get('outcome', '') or 'unknown'} | {item.get('created_at', '')}" for item in visible_calls}
    if call_labels:
        selected_call_id = st.selectbox("Select a call", list(call_labels.keys()), format_func=lambda item: call_labels.get(item, item), key="voice_call_detail_id")
        if selected_call_id:
            try:
                detail_response = api_request("GET", f"/voice/calls/{selected_call_id}", timeout=8)
                detail = safe_voice_api_json(detail_response)
                if detail is None:
                    raise RuntimeError("Voice Calls API is not available right now.")
                if detail_response.status_code in {401, 403}:
                    raise RuntimeError("You are not authorized to view this voice call.")
                if detail_response.status_code == 404:
                    raise RuntimeError("Voice call was not found.")
                if not detail_response.is_success:
                    raise RuntimeError(str(detail.get("detail", "Voice call could not be loaded right now.")))
                with st.expander("Transcript", expanded=False):
                    st.markdown(f"**Outcome:** {html.escape(str(detail.get('outcome') or 'unknown'))}")
                    st.markdown(f"**Summary:** {html.escape(str(detail.get('summary') or ''))}")
                    transcript = str(detail.get("transcript") or "").strip()
                    st.text_area("Transcript", value=transcript or "No transcript available.", height=220, disabled=True)
            except Exception as error:
                st.warning(f"Could not load selected call: {error}")

    try:
        leads_frame = load_dashboard_data(tenant)
    except Exception as error:
        st.warning(f"Could not load leads for voice campaign: {error}")
        leads_frame = pd.DataFrame()
    phone_ready = leads_frame[leads_frame["phone"].astype(str).str.strip() != ""] if not leads_frame.empty and "phone" in leads_frame else pd.DataFrame()
    lead_labels = {
        str(row["id"]): f"{str(row.get('company') or row.get('company_url') or row['id'])[:70]}"
        for _, row in phone_ready.head(100).iterrows()
        if str(row.get("id", "")).strip()
    }
    st.markdown('<div class="section-title">Start Voice Campaign</div>', unsafe_allow_html=True)
    selected_leads = st.multiselect(
        "Choose up to 5 leads with phone numbers",
        list(lead_labels.keys()),
        format_func=lambda item: lead_labels.get(item, item),
        max_selections=5,
        key="voice_campaign_leads",
    )
    if st.button("Start Voice Campaign", key="start_voice_campaign", use_container_width=True, disabled=not selected_leads):
        try:
            response = api_request("POST", "/voice/campaign", json={"lead_ids": selected_leads[:5], "max_calls": min(5, len(selected_leads))}, timeout=10)
            payload = response.json()
            if not response.is_success:
                raise RuntimeError(str(payload.get("detail", "Could not start voice campaign.")))
            load_voice_calls.clear()
            st.success(f"Voice campaign queued: {payload.get('job_id', '')} ({payload.get('status', 'queued')})")
        except Exception as error:
            st.warning(f"Could not start voice campaign: {error}")

    single_lead_id = st.selectbox(
        "Single call lead",
        [""] + list(lead_labels.keys()),
        format_func=lambda item: "Choose a lead" if not item else lead_labels.get(item, item),
        key="voice_single_call_lead",
    )
    if st.button("Start Single Call", key="start_single_voice_call", use_container_width=True, disabled=not single_lead_id):
        try:
            response = api_request("POST", f"/voice/call/{single_lead_id}", timeout=10)
            payload = response.json()
            if not response.is_success:
                raise RuntimeError(str(payload.get("detail", "Could not start voice call.")))
            load_voice_calls.clear()
            st.success(f"Voice call started: {payload.get('vapi_call_id', '')}")
        except Exception as error:
            st.warning(f"Could not start voice call: {error}")


def render_lead_action_buttons(row: pd.Series, index: int) -> None:
    lead_id = str(row.get("id", "") or "").strip()
    if not lead_id:
        st.warning("This lead is missing an ID, so advanced tools cannot save outputs yet.")
        return

    agency_kit = row.get("agency_kit", {}) if isinstance(row.get("agency_kit", {}), dict) else {}
    offer_match = row.get("offer_match", {}) if isinstance(row.get("offer_match", {}), dict) else {}
    whatsapp_sales_kit = row.get("whatsapp_sales_kit", {}) if isinstance(row.get("whatsapp_sales_kit", {}), dict) else {}

    status_cols = st.columns(3)
    for label, status, col in [
        ("Mark as Replied", "replied", status_cols[0]),
        ("Mark as Interested", "interested", status_cols[1]),
        ("Mark as Not Interested", "not_interested", status_cols[2]),
    ]:
        with col:
            if st.button(label, key=f"reply_status_{status}_{lead_id}_{index}", use_container_width=True):
                response = api_request("POST", f"/leads/{lead_id}/reply-status", json={"reply_status": status})
                payload = parse_api_json(response)
                if response.is_success:
                    st.success("Reply status updated.")
                    st.rerun()
                else:
                    st.error(str(payload.get("detail", "Could not update reply status.")))

    cols = st.columns(3)
    with cols[0]:
        if st.button("AI Agency Kit", key=f"live_agency_kit_{lead_id}_{index}", use_container_width=True):
            response = api_request("POST", f"/leads/{lead_id}/agency-kit", json={})
            payload = parse_api_json(response)
            if response.is_success:
                st.success("Agency Kit generated.")
                st.rerun()
            else:
                st.error(str(payload.get("detail", "Could not generate Agency Kit.")))
    with cols[1]:
        if st.button("Offer Matchmaker", key=f"live_offer_match_{lead_id}_{index}", use_container_width=True):
            response = api_request("POST", f"/leads/{lead_id}/offer-match", json={})
            payload = parse_api_json(response)
            if response.is_success:
                remember_offer_match(payload.get("offer_match", {}), str(row.get("company_url", "") or lead_id))
                st.success("Offer match generated.")
                st.rerun()
            else:
                st.error(str(payload.get("detail", "Could not generate offer match.")))
    with cols[2]:
        if st.button("WhatsApp Sales Kit", key=f"live_whatsapp_sales_{lead_id}_{index}", use_container_width=True):
            response = api_request("POST", f"/leads/{lead_id}/whatsapp-sales-kit", json={})
            payload = parse_api_json(response)
            if response.is_success:
                remember_whatsapp_sales_kit(payload.get("whatsapp_sales_kit", {}), str(row.get("company_url", "") or lead_id))
                st.success("WhatsApp Sales Kit generated.")
                st.rerun()
            else:
                st.error(str(payload.get("detail", "Could not generate WhatsApp Sales Kit.")))

    with st.expander("AI Agency Kit details", expanded=bool(agency_kit)):
        render_agency_kit_details(agency_kit)
    with st.expander("Offer Matchmaker details", expanded=bool(offer_match)):
        render_offer_match_output(offer_match)
    with st.expander("WhatsApp Sales Kit details", expanded=bool(whatsapp_sales_kit)):
        render_whatsapp_sales_output(whatsapp_sales_kit)


def render_live_leads_page(frame: pd.DataFrame) -> None:
    st.markdown('<div class="page-section page-card"><div class="page-card-inner dashboard-actions">', unsafe_allow_html=True)
    header_cols = st.columns([2, 1, 1])
    with header_cols[0]:
        st.markdown('<div class="section-title">Leads</div>', unsafe_allow_html=True)
        st.markdown('<div class="section-caption">Review saved leads without CRM clutter.</div>', unsafe_allow_html=True)
    with header_cols[1]:
        if frame.empty:
            st.button("Export CSV", use_container_width=True, disabled=True)
        else:
            csv_frame = sanitize_csv_frame(frame[[column for column in STANDARD_LEAD_EXPORT_COLUMNS if column in frame.columns]])
            st.download_button(
                "Export CSV",
                csv_frame.to_csv(index=False).encode("utf-8"),
                file_name="leads.csv",
                mime="text/csv",
                use_container_width=True,
            )
    with header_cols[2]:
        quality_frame = load_lead_quality_export() if not frame.empty else pd.DataFrame()
        if quality_frame.empty:
            st.button("Quality Report", use_container_width=True, disabled=True)
        else:
            report = sanitize_csv_frame(quality_frame)
            st.download_button(
                "Quality Report",
                report.to_csv(index=False).encode("utf-8"),
                file_name="lead_quality_report.csv",
                mime="text/csv",
                use_container_width=True,
            )
    if frame.empty:
        render_empty_state("No leads yet", "Enter a niche and country to generate your first leads.", "Generate Leads")
        st.markdown("</div></div>", unsafe_allow_html=True)
        return

    st.caption(f"Showing {len(frame)} lead rows.")
    summary_columns = [
        "company_url",
        "verified_email",
        "phone",
        "readiness",
        "score",
        "outreach_status",
        "lead_reason",
    ]
    list_frame = frame.copy()
    if "lead_reason" in list_frame.columns:
        list_frame["lead_reason"] = list_frame["lead_reason"].astype(str).map(lambda value: truncate_text(value, 120))
    render_table_page(list_frame, summary_columns, show_csv_export=False)

    for index, row in frame.head(50).iterrows():
        company = str(row.get("company", "") or row.get("company_url", "") or row.get("verified_email", "") or row.get("phone", "") or "Lead").strip()
        score = int(row.get("score", 0) or 0)
        reason = str(row.get("lead_reason", "") or row.get("service_reason", "") or "").strip()
        with st.expander(f"{company[:90]} - Score {score}", expanded=False):
            lead_cols = st.columns(4)
            lead_cols[0].write(f"Domain: {row.get('company_url', '') or 'Not available'}")
            lead_cols[1].write(f"Email: {row.get('verified_email', '') or row.get('likely_email', '') or 'Not found'}")
            lead_cols[2].write(f"Phone: {row.get('phone', '') or 'Not found'}")
            lead_cols[3].write(f"Outreach: {row.get('outreach_status', '') or 'pending'}")
            st.write(f"Lead reason: {truncate_text(reason, 120) or 'Not available'}")
            if reason and len(reason) > 120 and st.checkbox("Show full reason", key=f"full_reason_{index}"):
                st.write(reason)
    st.markdown("</div></div>", unsafe_allow_html=True)


def render_agency_kit_details(kit: Dict[str, Any]) -> None:
    if not kit:
        render_empty_state("No Agency Kit yet", "Generate a kit to see service recommendations, outreach copy, and proposal structure.")
        return
    col_service, col_channel, col_score = st.columns([2, 1, 1])
    col_service.markdown(f"**Recommended service**\n\n{kit.get('recommended_service', '')}")
    col_channel.markdown(f"**Channel**\n\n{kit.get('recommended_channel', '')}")
    col_score.markdown(f"**Confidence**\n\n{kit.get('confidence_score', 0)}/100")
    st.markdown(f"**Offer angle**\n\n{kit.get('offer_angle', '')}")
    with st.expander("Outreach email", expanded=True):
        st.code(str(kit.get("outreach_email", "")), language=None)
    with st.expander("WhatsApp / call script", expanded=False):
        st.code(str(kit.get("whatsapp_or_call_script", "")), language=None)

    followups = kit.get("followup_sequence", []) or []
    if followups:
        with st.expander("Follow-up sequence", expanded=False):
            for item in followups:
                st.write(f"- {item}")

    proposal = kit.get("proposal_outline", {}) or {}
    if proposal:
        with st.expander("Proposal outline", expanded=False):
            st.write(f"Problem: {proposal.get('problem', '')}")
            st.write(f"Solution: {proposal.get('solution', '')}")
            st.write(f"Timeline: {proposal.get('timeline', '')}")
            st.write(f"Pricing angle: {proposal.get('pricing_angle', '')}")
            st.write(f"Next step: {proposal.get('next_step', '')}")

    landing_copy = kit.get("landing_page_copy", {}) or {}
    if landing_copy:
        with st.expander("Landing page copy", expanded=False):
            st.write(f"Headline: {landing_copy.get('headline', '')}")
            st.write(f"Subheadline: {landing_copy.get('subheadline', '')}")
            for item in landing_copy.get("bullets", []) or []:
                st.write(f"- {item}")
            st.write(f"CTA: {landing_copy.get('cta', '')}")

    st.markdown(f"**Next action**\n\n{kit.get('next_action', '')}")


def render_agency_kit_panel(frame: pd.DataFrame) -> None:
    st.markdown('<div class="page-section page-card"><div class="page-card-inner dashboard-actions">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">AI Agency Kit</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="section-caption">Rule-based fallback kits. Free: 3/month, Pro: 100/month, Agency: unlimited fair-use.</div>',
        unsafe_allow_html=True,
    )
    if frame.empty:
        st.info("Generate leads first, then create Agency Kits from this page.")
        st.markdown("</div></div>", unsafe_allow_html=True)
        return

    for index, row in frame.head(25).iterrows():
        lead_id = str(row.get("id", "") or "").strip()
        company = str(row.get("company_url", "") or row.get("verified_email", "") or "Lead").strip()
        country = str(row.get("country", "") or "").strip()
        agency_kit = row.get("agency_kit", {}) if isinstance(row.get("agency_kit", {}), dict) else {}
        label_prefix = "View Agency Kit" if agency_kit else "Generate Agency Kit"
        expander_label = f"{label_prefix}: {company[:80]}{f' - {country}' if country else ''}"
        with st.expander(expander_label, expanded=False):
            if not lead_id:
                st.warning("This lead is missing an ID, so an Agency Kit cannot be saved yet.")
                continue
            button_label = "Regenerate Agency Kit" if agency_kit else "Generate Agency Kit"
            if st.button(button_label, key=f"agency_kit_{lead_id}_{index}", use_container_width=True):
                response = api_request("POST", f"/leads/{lead_id}/agency-kit", json={})
                payload = parse_api_json(response)
                if response.is_success:
                    st.success("Agency Kit generated.")
                    st.rerun()
                else:
                    st.error(str(payload.get("detail", "Could not generate Agency Kit.")))
            render_agency_kit_details(agency_kit)
    st.markdown("</div></div>", unsafe_allow_html=True)


def sanitize_csv_frame(frame: pd.DataFrame) -> pd.DataFrame:
    protected = frame.copy()
    dangerous_prefixes = ("=", "+", "-", "@")
    for column in protected.columns:
        protected[column] = protected[column].map(
            lambda value: f"'{value}" if isinstance(value, str) and value.startswith(dangerous_prefixes) else value
        )
    return protected


def render_settings_page() -> None:
    render_system_health_settings()
    render_ai_provider_settings()
    render_email_personalization_settings()
    render_gmail_settings()
    render_account_plan_settings()


def render_email_personalization_settings() -> None:
    st.markdown('<div class="page-section page-card"><div class="page-card-inner">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Personalized Email Setup</div>', unsafe_allow_html=True)
    st.caption("Tell Lead Hunter AI how to write outreach emails for your business.")
    try:
        response = api_request("GET", "/settings/email-personalization")
        payload = parse_api_json(response)
        config = dict(payload.get("config", {}) or {}) if response.is_success else {}
    except Exception:
        config = {}
    tone_options = ["Professional", "Friendly", "Confident", "Warm", "Direct", "Premium", "Casual", "Formal"]
    goal_options = ["Book a meeting", "Offer free audit", "Introduce services", "Start conversation", "Follow up", "Demo invitation"]
    language_options = ["English", "Roman Urdu", "Arabic", "Simple English", "Custom"]
    with st.form("email_personalization_form"):
        c1, c2 = st.columns(2)
        sender_name = c1.text_input("Sender name", value=str(config.get("sender_name", "") or ""), placeholder="Abdullah Zahoor")
        brand_name = c2.text_input("Company / brand name", value=str(config.get("brand_name", "") or ""), placeholder="Lead Hunter AI")
        services = st.text_area(
            "Services offered",
            value=str(config.get("services_offered", "") or ""),
            placeholder="AI lead generation, website automation, chatbot development, marketing automation",
        )
        target_customer = st.text_input(
            "Target customer type",
            value=str(config.get("target_customer_type", "") or ""),
            placeholder="software houses, agencies, consultants, dentists, real estate companies",
        )
        c3, c4, c5 = st.columns(3)
        tone = c3.selectbox("Tone", tone_options, index=tone_options.index(str(config.get("tone", "Professional") or "Professional")) if str(config.get("tone", "Professional") or "Professional") in tone_options else 0)
        goal = c4.selectbox("Email goal", goal_options, index=goal_options.index(str(config.get("email_goal", "Start conversation") or "Start conversation")) if str(config.get("email_goal", "Start conversation") or "Start conversation") in goal_options else 3)
        language = c5.selectbox("Language", language_options, index=language_options.index(str(config.get("language", "English") or "English")) if str(config.get("language", "English") or "English") in language_options else 0)
        cta = st.text_input("CTA", value=str(config.get("cta", "") or ""), placeholder="Would you be open to a quick 10-minute call this week?")
        signature = st.text_area("Optional signature", value=str(config.get("signature", "") or ""), placeholder="Abdullah Zahoor\nFounder, Lead Hunter AI")
        save_clicked = st.form_submit_button("Save settings", use_container_width=True)
    body = {
        "sender_name": sender_name,
        "brand_name": brand_name,
        "services_offered": services,
        "target_customer_type": target_customer,
        "tone": tone,
        "email_goal": goal,
        "cta": cta,
        "language": language,
        "signature": signature,
    }
    if save_clicked:
        if not sender_name.strip() or not services.strip():
            st.warning("Sender name and services offered are recommended for stronger personalization.")
        response = api_request("POST", "/settings/email-personalization", json=body)
        payload = parse_api_json(response)
        if response.is_success:
            st.success("Email personalization saved.")
        else:
            st.error(str(payload.get("detail", "Could not save email setup.")))
    preview_col, reset_col = st.columns(2)
    with preview_col:
        preview_clicked = st.button("Generate sample email", use_container_width=True)
    with reset_col:
        reset_clicked = st.button("Reset to defaults", use_container_width=True)
    if preview_clicked:
        response = api_request("POST", "/settings/email-personalization/preview", json=body)
        payload = parse_api_json(response)
        sample = dict(payload.get("sample", {}) or {})
        if response.is_success and sample:
            st.markdown(f"**Subject:** {html.escape(str(sample.get('subject', '') or ''))}")
            st.text_area("Sample body", value=str(sample.get("body", "") or ""), height=180)
        else:
            st.error(str(payload.get("detail", "Could not generate sample email.")))
    if reset_clicked:
        defaults = {
            "sender_name": "",
            "brand_name": "",
            "services_offered": "",
            "target_customer_type": "",
            "tone": "Professional",
            "email_goal": "Start conversation",
            "cta": "",
            "language": "English",
            "signature": "",
        }
        response = api_request("POST", "/settings/email-personalization", json=defaults)
        payload = parse_api_json(response)
        if response.is_success:
            st.success("Email personalization reset to defaults.")
            st.rerun()
        else:
            st.error(str(payload.get("detail", "Could not reset email setup.")))
    st.markdown("</div></div>", unsafe_allow_html=True)


def render_ai_provider_settings() -> None:
    provider_defaults = {
        "fallback": "",
        "openai": "gpt-4o-mini",
        "anthropic": "claude-3-5-haiku-latest",
        "gemini": "gemini-1.5-flash",
        "groq": "llama-3.1-8b-instant",
        "openrouter": "openai/gpt-4o-mini",
    }
    providers = list(provider_defaults.keys())
    st.markdown(
        """
        <div class="page-section dashboard-actions">
            <div class="page-card">
                <div class="page-card-inner">
                    <div class="section-title">AI Provider Settings</div>
                    <div class="settings-note">Choose an optional AI provider for enhanced outputs. Rule-based fallback stays available without an API key.</div>
        """,
        unsafe_allow_html=True,
    )
    try:
        status_response = api_request("GET", "/settings/providers/ai/status")
        status = parse_api_json(status_response)
    except Exception as error:
        st.error(f"Could not load AI provider status: {error}")
        status = {"configured": False, "provider": "fallback", "model": "", "enabled": False}
    configured = bool(status.get("configured"))
    current_provider = str(status.get("provider") or "fallback").strip().lower()
    if current_provider not in providers:
        current_provider = "fallback"
    current_model = str(status.get("model") or provider_defaults[current_provider]).strip()
    enabled = bool(status.get("enabled", False))
    status_class = "success" if configured else "warning"
    st.markdown(
        f'<span class="status-badge {status_class}">{"Configured" if configured else "Fallback only"}</span>',
        unsafe_allow_html=True,
    )
    st.caption(f"Provider: {current_provider} | Model: {current_model or 'none'} | Enabled: {'yes' if enabled else 'no'}")
    st.caption("Fallback mode works without any API key.")

    with st.form("ai_provider_settings_form"):
        selected_provider = st.selectbox("Provider", providers, index=providers.index(current_provider))
        suggested_model = provider_defaults.get(selected_provider, "")
        api_key = st.text_input("API Key", value="", type="password", placeholder="Stored securely; leave blank to keep existing key")
        model = st.text_input("Model", value=current_model or suggested_model, placeholder=suggested_model or "No model required")
        enabled_input = st.checkbox("Enabled", value=enabled or selected_provider == "fallback")
        submitted = st.form_submit_button("Save AI Provider", use_container_width=True)
    if submitted:
        response = api_request(
            "POST",
            "/settings/providers/ai",
            json={
                "provider": selected_provider,
                "api_key": api_key.strip(),
                "model": model.strip() or suggested_model,
                "enabled": enabled_input,
            },
        )
        body = parse_api_json(response)
        if response.is_success:
            st.success("AI provider settings saved.")
            st.rerun()
        else:
            st.error(str(body.get("detail", "Could not save AI provider settings.")))

    if st.button("Test Connection", use_container_width=True):
        response = api_request("POST", "/settings/providers/ai/test", json={})
        body = parse_api_json(response)
        if response.is_success and body.get("success"):
            st.success("AI provider responded successfully.")
        else:
            st.warning(str(body.get("message", "AI provider test failed safely.")))
    st.markdown("</div></div></div>", unsafe_allow_html=True)


def render_gmail_settings() -> None:
    st.markdown(
        """
        <div class="page-section dashboard-actions">
            <div class="page-card">
                <div class="page-card-inner">
                    <div class="section-title">Gmail Automation Setup</div>
                    <div class="settings-note">Connect Gmail to send outreach and monitor replies. You can disconnect anytime.</div>
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
        status = {"configured": False, "connected": False, "sender_email": ""}
    configured = bool(status.get("configured"))
    connected = bool(status.get("connected"))
    sender_email = str(status.get("sender_email", "") or "")
    status_label = str(status.get("status_label", "") or ("Connected" if connected else "Missing credentials"))
    status_code = str(status.get("status", "") or "").strip()
    last_successful_send = str(status.get("last_successful_send", "") or "")
    st.markdown(
        f'<span class="status-badge {"success" if connected else "warning"}">{html.escape(status_label)}</span>',
        unsafe_allow_html=True,
    )
    if connected and sender_email:
        st.caption(f"Connected as {sender_email}")
        if last_successful_send:
            st.caption(f"Last successful send: {last_successful_send}")
    elif status_code == "gmail_api_disabled":
        st.caption("Gmail API is disabled for this OAuth project. Enable Gmail API, then reconnect Gmail.")
    elif status_code == "invalid_credentials":
        st.caption("Gmail credentials are invalid or incomplete. Reconnect Gmail.")
    elif configured:
        st.caption("Gmail is configured, but the sender email could not be loaded.")
    else:
        st.caption("Use Google OAuth to connect a sender account.")

    with st.expander("Gmail setup guide", expanded=not connected):
        st.markdown(
            "- Enable the Gmail API in the Google Cloud project used for OAuth.\n"
            "- Add the Gmail OAuth redirect URL shown in your deployment settings.\n"
            "- Connect Gmail from this screen and approve `gmail.send` and `gmail.readonly`.\n"
            "- If Google does not return offline access, click Reconnect Gmail and approve access again.\n"
            "- In demo mode, Gmail can be connected and checked, but no real email is sent."
        )

    connect_label = "Reconnect Gmail" if connected else "Connect Gmail"
    if st.button(connect_label, use_container_width=True):
        response = api_request("GET", "/settings/providers/gmail/oauth/start")
        body = parse_api_json(response)
        if response.is_success:
            st.session_state["gmail_oauth_url"] = str(body.get("authorization_url", "") or "")
            st.success("Google authorization link is ready.")
        else:
            st.error(str(body.get("detail", "Could not start Gmail connection.")))

    gmail_oauth_url = str(st.session_state.get("gmail_oauth_url", "") or "")
    if gmail_oauth_url:
        if hasattr(st, "link_button"):
            st.link_button("Continue to Google", gmail_oauth_url, use_container_width=True)
        else:
            safe_url = html.escape(gmail_oauth_url, quote=True)
            st.markdown(f'<a href="{safe_url}" target="_blank" rel="noopener">Continue to Google</a>', unsafe_allow_html=True)

    if connected and st.button("Disconnect Gmail", use_container_width=True):
        response = api_request("POST", "/settings/providers/gmail/disconnect", json={})
        body = parse_api_json(response)
        if response.is_success:
            st.session_state.pop("gmail_oauth_url", None)
            st.success("Gmail disconnected.")
            st.rerun()
        else:
            st.error(str(body.get("detail", "Could not disconnect Gmail.")))
    st.markdown("</div></div></div>", unsafe_allow_html=True)


def render_system_health_settings() -> None:
    st.markdown(
        """
        <div class="page-section dashboard-actions">
            <div class="page-card">
                <div class="page-card-inner">
                    <div class="section-title">System Health</div>
                    <div class="settings-note">Run a quick pre-demo check for providers, queue, database, Gmail, and outreach safety.</div>
        """,
        unsafe_allow_html=True,
    )
    if st.button("Run System Health Check", use_container_width=True):
        st.session_state["system_health_result"] = load_system_health()
    health = st.session_state.get("system_health_result", {})
    if isinstance(health, dict) and health:
        st.caption(f"Overall status: {str(health.get('status', 'unknown')).upper()}")
        if bool(health.get("demo_mode")):
            st.info("Demo mode is enabled. Real Gmail sends are blocked.")
        rows = []
        for name, payload in dict(health.get("checks", {}) or {}).items():
            detail = dict(payload or {})
            rows.append(
                {
                    "check": str(name).replace("_", " ").title(),
                    "status": str(detail.get("status", "") or ""),
                    "message": str(detail.get("message", "") or ""),
                }
            )
        if rows:
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    st.markdown("</div></div></div>", unsafe_allow_html=True)


def render_account_plan_settings() -> None:
    auth = st.session_state.get("auth", {}) or {}
    snapshot: Dict[str, Any] = {}
    try:
        snapshot = load_dashboard_snapshot_cached()
    except Exception:
        snapshot = {}
    st.markdown('<div class="page-section page-card"><div class="page-card-inner">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Account / Plan</div>', unsafe_allow_html=True)
    st.markdown('<div class="section-caption">Current workspace plan and lightweight usage summary.</div>', unsafe_allow_html=True)
    cards = [
        ("Workspace", str(auth.get("tenant_id", "") or "Workspace"), "Tenant ID"),
        ("Current plan", current_plan(), infer_subscription_status().replace("_", " ").title()),
        ("Saved leads", int(snapshot.get("lead_count", 0) or 0), "This workspace"),
        ("Jobs", int(snapshot.get("job_count", 0) or 0), "Queued and completed"),
    ]
    st.markdown(
        '<div class="metric-card-grid">'
        + "".join(render_metric_card(label, value, help_text) for label, value, help_text in cards)
        + "</div>",
        unsafe_allow_html=True,
    )
    if not has_pro_features():
        st.info("Upgrade to Pro for Gmail automation, outreach, followups, and reply tracking.")
    st.markdown("</div></div>", unsafe_allow_html=True)


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


def render_module_header(title: str, subtitle: str) -> None:
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
                <h1 class="hero-title">{html.escape(title)}</h1>
                <p class="hero-note">{html.escape(subtitle)}</p>
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


def render_dashboard_header() -> None:
    render_module_header(
        "Email CRM",
        "Find leads, manage contacts, generate agency kits, and automate outreach.",
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
    st.subheader("Payment Request History")
    try:
        response = api_request("GET", "/billing/payment-requests/me")
        payload = parse_api_json(response)
    except Exception as error:
        st.error(f"Could not load payment requests: {error}")
        return
    items = payload.get("items", []) if response.is_success else []
    if not items:
        st.info("No payment requests yet.")
        return
    rows = []
    for item in items:
        rows.append(
            {
                "Plan": item.get("plan", ""),
                "Status": str(item.get("status", "")).replace("_", " ").title(),
                "Amount": f"{item.get('currency', 'PKR')} {item.get('amount', 0)}",
                "Submitted": item.get("created_at", ""),
                "Reviewed": item.get("reviewed_at", ""),
                "Admin note": item.get("admin_note", ""),
            }
        )
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


def render_subscribe_section() -> None:
    st.subheader("Request Upgrade")
    default_plan = current_plan()
    try:
        plans_response = api_request("GET", "/billing/plans")
        plans_payload = parse_api_json(plans_response)
    except Exception:
        plans_payload = {"items": [], "payment": {}}
    plan_items = plans_payload.get("items", []) or []
    if plan_items:
        cols = st.columns(min(4, len(plan_items)))
        for index, item in enumerate(plan_items):
            with cols[index % len(cols)]:
                st.metric(str(item.get("name", "")), f"{plans_payload.get('currency', 'PKR')} {item.get('price', 0)}")
                limits = item.get("limits", {}) or {}
                lead_limit = "Unlimited" if int(limits.get("monthly_leads", 0) or 0) >= 999999999 else str(limits.get("monthly_leads", 0))
                email_limit = "Unlimited" if int(limits.get("monthly_emails", 0) or 0) >= 999999999 else str(limits.get("monthly_emails", 0))
                st.caption(f"Leads: {lead_limit} | Emails: {email_limit}")
                st.caption("Agency Mode included" if item.get("agency_mode") else "Standard workspace")
    payment = plans_payload.get("payment", {}) or {}
    plan_names = [str(item.get("name", "")) for item in plan_items if str(item.get("name", "")) and str(item.get("name", "")) != "Free"] or ["Pro", "Agency"]
    default_index = plan_names.index(default_plan) if default_plan in plan_names else 0
    selected_plan = st.selectbox("Selected plan", plan_names, index=default_index, key="billing_selected_plan")
    selected_plan_item = next((item for item in plan_items if str(item.get("name", "")) == selected_plan), {})
    with st.expander("Payment instructions", expanded=True):
        st.write(f"Method: {payment.get('method_name', '')}")
        st.write(f"Account title: {payment.get('account_title', '')}")
        st.write(f"Account number: {payment.get('account_number', '')}")
        st.write(f"Amount: {plans_payload.get('currency', 'PKR')} {selected_plan_item.get('price', 0)}")
        qr_path = str(selected_plan_item.get("qr_path") or payment.get("qr_path") or DEFAULT_PAYMENT_QR_PATH)
        local_qr_path = resolve_local_asset_path(qr_path)
        if local_qr_path.exists():
            st.image(str(local_qr_path), caption=f"{selected_plan} Payment QR", width=220)
        elif payment.get("qr_code_url"):
            st.image(str(payment.get("qr_code_url")), caption="Payment QR", width=180)
    with st.form("payment_request_form"):
        full_name = st.text_input("Full name", value=str(st.session_state.get("auth", {}).get("full_name", "") or ""))
        phone = st.text_input("Phone / WhatsApp number")
        payment_method = st.selectbox("Payment method", ["JazzCash", "EasyPaisa", "Bank Transfer", "Nayapay", "Sadapay", "Other"])
        transaction_reference = st.text_input("Transaction / reference ID (optional)")
        screenshot = st.file_uploader("Payment screenshot", type=["png", "jpg", "jpeg", "webp"])
        note = st.text_area("Optional note")
        submitted = st.form_submit_button("Submit Payment Request", use_container_width=True)
    if submitted:
        if screenshot is None or not phone.strip() or not full_name.strip():
            st.error("Full name, phone number, and payment screenshot are required.")
            return
        mime_type = screenshot.type or mimetypes.guess_type(screenshot.name)[0] or "application/octet-stream"
        files = {"screenshot": (screenshot.name, screenshot.getvalue(), mime_type)}
        data = {
            "full_name": full_name.strip(),
            "phone_number": phone.strip(),
            "selected_plan": selected_plan,
            "payment_method": payment_method,
            "transaction_reference": transaction_reference.strip(),
            "user_note": note.strip(),
        }
        with st.spinner("Submitting payment request..."):
            try:
                response = api_request("POST", "/billing/payment-requests", data=data, files=files)
                payload = parse_api_json(response)
                if response.is_success:
                    st.success("Payment request submitted. Admin will review it soon.")
                else:
                    st.error(str(payload.get("detail", "Payment request failed.")))
            except Exception as error:
                st.error(str(error))


def render_billing_page() -> None:
    render_subscription_card()
    st.markdown('<div class="page-section page-card"><div class="page-card-inner">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Billing</div>', unsafe_allow_html=True)
    st.markdown('<div class="section-caption">Plan selection, subscription requests, and payment proof uploads.</div>', unsafe_allow_html=True)
    render_subscribe_section()
    render_payment_history()
    st.markdown("</div></div>", unsafe_allow_html=True)


def render_admin_payments() -> None:
    st.subheader("Payment Requests")
    status_filter = st.selectbox(
        "Payment status",
        ["", "pending", "needs_review", "approved", "rejected"],
        format_func=lambda value: "All" if not value else value.replace("_", " ").title(),
    )
    try:
        response = api_request("GET", "/admin/payment-requests", params={"status": status_filter} if status_filter else None)
        payload = parse_api_json(response)
    except Exception as error:
        st.error(f"Could not load payment requests: {error}")
        return
    items = payload.get("items", []) if response.is_success else []
    if not items:
        st.info("No payment requests found.")
        return
    for item in items[:50]:
        label = f"{item.get('user_email', '')} - {item.get('plan', '')} - {str(item.get('status', '')).replace('_', ' ').title()}"
        with st.expander(label, expanded=str(item.get("status", "")) in {"pending", "needs_review"}):
            cols = st.columns(3)
            cols[0].write(f"Tenant: {item.get('tenant_id', '')}")
            cols[1].write(f"Phone: {item.get('phone_number', '')}")
            cols[2].write(f"Amount: {item.get('currency', 'PKR')} {item.get('amount', 0)}")
            st.write(f"Method: {item.get('payment_method', '')}")
            st.write(f"Transaction ref: {item.get('transaction_reference', '') or 'Not provided'}")
            if item.get("user_note"):
                st.text_area("User note", value=str(item.get("user_note", "")), height=80, disabled=True, key=f"user_note_{item.get('id')}")
            if item.get("admin_note"):
                st.text_area("Admin note", value=str(item.get("admin_note", "")), height=80, disabled=True, key=f"admin_note_existing_{item.get('id')}")
            st.caption("Screenshot uploaded." if item.get("has_screenshot") else "No screenshot attached.")
            payment_id = str(item.get("id", ""))
            note = st.text_input("Review note", key=f"payment_review_note_{payment_id}")
            action_cols = st.columns(3)
            if action_cols[0].button("Approve", key=f"approve_payment_{payment_id}", disabled=str(item.get("status")) == "approved"):
                result = api_request("POST", f"/admin/payment-requests/{payment_id}/approve", json={"admin_note": note})
                body = parse_api_json(result)
                if result.is_success:
                    st.success("Payment approved and tenant plan updated.")
                    st.rerun()
                else:
                    st.error(str(body.get("detail", "Approval failed.")))
            if action_cols[1].button("Needs review", key=f"needs_review_payment_{payment_id}"):
                result = api_request("POST", f"/admin/payment-requests/{payment_id}/needs-review", json={"admin_note": note})
                body = parse_api_json(result)
                if result.is_success:
                    st.success("Payment marked needs review.")
                    st.rerun()
                else:
                    st.error(str(body.get("detail", "Update failed.")))
            if action_cols[2].button("Reject", key=f"reject_payment_{payment_id}"):
                result = api_request("POST", f"/admin/payment-requests/{payment_id}/reject", json={"admin_note": note})
                body = parse_api_json(result)
                if result.is_success:
                    st.success("Payment rejected.")
                    st.rerun()
                else:
                    st.error(str(body.get("detail", "Rejection failed.")))


def admin_get(path: str) -> Dict[str, Any]:
    response = api_request("GET", path)
    payload = parse_api_json(response)
    if not response.is_success:
        st.error(str(payload.get("detail", "Admin request failed.")))
        return {}
    return payload


def render_admin_summary() -> None:
    render_page_header("Admin Dashboard", "Workspace analytics, users, leads, jobs, and payment approvals.", "Admin")
    summary = admin_get("/admin/summary")
    outreach = admin_get("/admin/outreach/stats")
    if not summary:
        return
    primary_metrics = [
        ("Total users", int(summary.get("total_users", 0) or 0), "All workspaces"),
        ("Active tenants", int(summary.get("total_tenants", 0) or 0), "Current tenants"),
        ("Total leads", int(summary.get("total_leads", 0) or 0), "Generated leads"),
        ("Jobs", int(summary.get("total_jobs", 0) or 0), "All job records"),
    ]
    secondary_metrics = [
        ("Emails sent", int(summary.get("total_emails_sent", 0) or 0), "Outbound"),
        ("Replies", int(summary.get("total_replies", 0) or 0), "Tracked replies"),
        ("Active today", int(summary.get("active_users_today", 0) or 0), "Users"),
        ("Failed jobs", int(summary.get("failed_jobs", 0) or 0), "Needs review"),
    ]
    st.markdown('<div class="page-section page-card"><div class="page-card-inner">', unsafe_allow_html=True)
    st.markdown(
        '<div class="metric-card-grid">'
        + "".join(render_metric_card(label, value, help_text) for label, value, help_text in primary_metrics)
        + "</div>",
        unsafe_allow_html=True,
    )
    st.markdown(
        '<div class="page-section metric-card-grid">'
        + "".join(render_metric_card(label, value, help_text) for label, value, help_text in secondary_metrics)
        + "</div>",
        unsafe_allow_html=True,
    )
    st.caption(
        f"Queued jobs: {int(summary.get('queued_jobs', 0) or 0)} | "
        f"Running jobs: {int(summary.get('running_jobs', 0) or 0)} | "
        f"Failed jobs: {int(summary.get('failed_jobs', 0) or 0)}"
    )
    if outreach:
        outreach_metrics = [
            ("Pending verified", int(outreach.get("pending_verified_leads", 0) or 0), "Ready leads"),
            ("Sent emails", int(outreach.get("sent_emails", 0) or 0), "Outbound"),
            ("Blocked leads", int(outreach.get("blocked_leads", 0) or 0), "Needs fix"),
        ]
        st.markdown(
            '<div class="page-section metric-card-grid">'
            + "".join(render_metric_card(label, value, help_text) for label, value, help_text in outreach_metrics)
            + "</div>",
            unsafe_allow_html=True,
        )
        failed = dict(outreach.get("failed_emails_by_reason", {}) or {})
        if failed:
            st.dataframe(
                pd.DataFrame([{"reason": key, "count": value} for key, value in sorted(failed.items())]),
                use_container_width=True,
                hide_index=True,
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


def render_pro_action_page(title: str, caption: str, button_label: str, agent_name: str, payload: Dict[str, Any] | None = None) -> None:
    st.markdown('<div class="page-section page-card"><div class="page-card-inner dashboard-actions">', unsafe_allow_html=True)
    st.markdown(f'<div class="section-title">{html.escape(title)}</div>', unsafe_allow_html=True)
    st.markdown(f'<div class="section-caption">{html.escape(caption)}</div>', unsafe_allow_html=True)
    if not has_pro_features():
        st.markdown('<span class="premium-badge">Pro</span>', unsafe_allow_html=True)
        st.warning(LOCKED_FEATURE_MESSAGE)
        st.button(button_label, use_container_width=True, disabled=True)
        st.markdown("</div></div>", unsafe_allow_html=True)
        return
    outreach_preflight: Dict[str, Any] = {}
    if agent_name == "outreach":
        outreach_preflight = load_outreach_preflight()
        render_outreach_preflight_summary(outreach_preflight)
        render_add_test_lead_form()
    if st.button(button_label, use_container_width=True):
        if agent_name == "outreach":
            if not outreach_preflight:
                outreach_preflight = load_outreach_preflight()
            if int(outreach_preflight.get("sendable_count", 0) or 0) <= 0:
                st.warning(NO_VERIFIED_EMAIL_LEADS_MESSAGE)
                st.markdown("</div></div>", unsafe_allow_html=True)
                return
        with st.spinner("Queueing automation job..."):
            enqueue_job(agent_name, payload or {})
        st.success("Automation job queued.")
    st.markdown("</div></div>", unsafe_allow_html=True)


def lead_picker_options(frame: pd.DataFrame) -> Dict[str, str]:
    labels: Dict[str, str] = {}
    if frame.empty:
        return labels
    for _, row in frame.iterrows():
        lead_id = str(row.get("id", "") or "").strip()
        if not lead_id:
            continue
        primary = str(row.get("company_url") or row.get("verified_email") or "Lead").strip()
        industry = str(row.get("industry") or "").strip()
        country = str(row.get("country") or "").strip()
        detail = " / ".join(item for item in [industry, country] if item)
        labels[lead_id] = f"{primary[:70]}{' - ' + detail if detail else ''}"
    return labels


def current_offer_match() -> Dict[str, Any]:
    match = st.session_state.get("offer_match", {}) or {}
    return match if isinstance(match, dict) else {}


def remember_offer_match(match: Dict[str, Any], source: str) -> None:
    st.session_state["offer_match"] = dict(match or {})
    st.session_state["offer_match_source"] = source


def render_offer_match_output(match: Dict[str, Any]) -> None:
    if not match:
        render_empty_state("No offer match yet", "Choose a lead and generate the clearest service offer to sell first.", "Generate Offer Match")
        return
    source = str(st.session_state.get("offer_match_source", "") or "").strip()
    if source:
        st.caption(f"Latest offer match: {source}")
    st.markdown('<div class="page-section page-card"><div class="page-card-inner">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Recommended Offer</div>', unsafe_allow_html=True)
    st.markdown(f"**{match.get('recommended_offer', '')}**")
    cols = st.columns(3)
    cols[0].metric("Category", str(match.get("offer_category", "") or "").replace("_", " ").title())
    cols[1].metric("Best channel", str(match.get("best_channel", "") or "").replace("_", " ").title())
    cols[2].metric("Confidence", f"{int(match.get('confidence_score', 0) or 0)}%")
    st.markdown(f"**Why this offer**\n\n{match.get('why_this_offer', '')}")
    pains = match.get("business_pain", []) or []
    if pains:
        st.markdown("**Business pain**")
        for pain in pains:
            st.write(f"- {pain}")
    starter = match.get("starter_package", {}) or {}
    pro = match.get("pro_package", {}) or {}
    package_cols = st.columns(2)
    with package_cols[0]:
        st.markdown(f"**{starter.get('name', 'Starter package')}**")
        st.caption(str(starter.get("price_range", "")))
        for item in starter.get("deliverables", []) or []:
            st.write(f"- {item}")
    with package_cols[1]:
        st.markdown(f"**{pro.get('name', 'Pro package')}**")
        st.caption(str(pro.get("price_range", "")))
        for item in pro.get("deliverables", []) or []:
            st.write(f"- {item}")
    st.markdown("**Pitch angle**")
    st.code(str(match.get("pitch_angle", "")), language=None)
    st.markdown(f"**Next step:** {match.get('next_step', '')}")
    st.markdown("</div></div>", unsafe_allow_html=True)


def render_offer_matchmaker_page(tenant: TenantContext) -> None:
    frame = load_dashboard_data(tenant)
    labels = lead_picker_options(frame)
    st.markdown('<div class="page-section page-card"><div class="page-card-inner dashboard-actions">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Lead-to-Offer Matchmaker</div>', unsafe_allow_html=True)
    st.markdown('<div class="section-caption">Pick a lead and get the clearest service offer to sell first.</div>', unsafe_allow_html=True)
    if not labels:
        st.info("No leads found yet. Generate or import leads first.")
        st.markdown("</div></div>", unsafe_allow_html=True)
        render_offer_match_output(current_offer_match())
        return
    selected_id = st.selectbox("Choose a lead", list(labels.keys()), key="offer_match_lead_id", format_func=lambda item: labels.get(item, item))
    if st.button("Generate Offer Match", use_container_width=True):
        response = api_request("POST", f"/leads/{selected_id}/offer-match", json={})
        payload = parse_api_json(response)
        if response.is_success:
            match = payload.get("offer_match", {})
            remember_offer_match(match, labels.get(selected_id, selected_id))
            st.success("Offer match generated and saved to lead metadata.")
        else:
            st.error(str(payload.get("detail", "Could not generate offer match.")))
    st.markdown("</div></div>", unsafe_allow_html=True)
    render_offer_match_output(current_offer_match())


def current_whatsapp_sales_kit() -> Dict[str, Any]:
    kit = st.session_state.get("whatsapp_sales_kit", {}) or {}
    return kit if isinstance(kit, dict) else {}


def remember_whatsapp_sales_kit(kit: Dict[str, Any], source: str) -> None:
    st.session_state["whatsapp_sales_kit"] = dict(kit or {})
    st.session_state["whatsapp_sales_source"] = source


def render_whatsapp_sales_output(kit: Dict[str, Any]) -> None:
    if not kit:
        render_empty_state("No sales script yet", "Choose a lead and generate WhatsApp, follow-up, voice note, and call scripts.", "Generate WhatsApp Sales Kit")
        return
    source = str(st.session_state.get("whatsapp_sales_source", "") or "").strip()
    if source:
        st.caption(f"Latest sales kit: {source}")
    st.markdown('<div class="page-section page-card"><div class="page-card-inner">', unsafe_allow_html=True)
    st.metric("Recommended channel", str(kit.get("recommended_channel", "") or "").replace("_", " ").title())
    st.markdown("**WhatsApp opener**")
    st.code(str(kit.get("whatsapp_opener", "")), language=None)
    st.markdown("**Follow-ups**")
    st.code("\n\n".join(str(kit.get(key, "")) for key in ["followup_1", "followup_2", "followup_3"]), language=None)
    st.markdown("**Voice note script**")
    st.code(str(kit.get("voice_note_script", "")), language=None)
    call_script = kit.get("call_script", {}) or {}
    if call_script:
        st.markdown("**Call script**")
        st.code("\n".join(f"{label.replace('_', ' ').title()}: {value}" for label, value in call_script.items()), language=None)
    objections = kit.get("objection_replies", {}) or {}
    if objections:
        st.markdown("**Objection replies**")
        st.code("\n".join(f"{label.replace('_', ' ').title()}: {value}" for label, value in objections.items()), language=None)
    st.markdown(f"**Next step:** {kit.get('next_step', '')}")
    st.markdown("</div></div>", unsafe_allow_html=True)


def render_whatsapp_sales_kit_page(tenant: TenantContext) -> None:
    frame = load_dashboard_data(tenant)
    labels = lead_picker_options(frame)
    st.markdown('<div class="page-section page-card"><div class="page-card-inner dashboard-actions">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">WhatsApp Sales Script Generator</div>', unsafe_allow_html=True)
    st.markdown('<div class="section-caption">Generate a friendly opener, follow-ups, voice note, call script, and objection replies.</div>', unsafe_allow_html=True)
    if not labels:
        st.info("No leads found yet. Generate or import leads first.")
        st.markdown("</div></div>", unsafe_allow_html=True)
        render_whatsapp_sales_output(current_whatsapp_sales_kit())
        return
    selected_id = st.selectbox("Choose a lead", list(labels.keys()), key="whatsapp_sales_lead_id", format_func=lambda item: labels.get(item, item))
    if st.button("Generate WhatsApp Sales Kit", use_container_width=True):
        response = api_request("POST", f"/leads/{selected_id}/whatsapp-sales-kit", json={})
        payload = parse_api_json(response)
        if response.is_success:
            kit = payload.get("whatsapp_sales_kit", {})
            remember_whatsapp_sales_kit(kit, labels.get(selected_id, selected_id))
            st.success("WhatsApp Sales Kit generated and saved to lead metadata.")
        else:
            st.error(str(payload.get("detail", "Could not generate WhatsApp Sales Kit.")))
    st.markdown("</div></div>", unsafe_allow_html=True)
    render_whatsapp_sales_output(current_whatsapp_sales_kit())


def email_crm_default_rows(frame: pd.DataFrame, sort_order: str = "Newest first", status_filter: str = "All", limit: int = 10) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    filtered = frame.copy()
    status_map = {
        "Sent": ("outreach_status", "sent"),
        "Failed": ("outreach_status", "failed"),
        "Replied": ("reply_status", "replied"),
        "No Reply": ("reply_status", "no_reply"),
    }
    if status_filter in status_map:
        column, value = status_map[status_filter]
        if column in filtered.columns:
            statuses = filtered[column].astype(str).str.strip().str.lower()
            filtered = filtered[statuses.ne("no_reply")] if status_filter == "Replied" else filtered[statuses.eq(value)]
    if "created_at" in filtered.columns:
        order = pd.to_datetime(filtered["created_at"], errors="coerce").sort_values(ascending=sort_order == "Oldest first")
        filtered = filtered.loc[order.index]
    return filtered.head(max(1, int(limit or 10)))


def render_email_crm_table(title: str, frame: pd.DataFrame, columns: List[str]) -> None:
    st.markdown(f'<div class="section-title">{html.escape(title)}</div>', unsafe_allow_html=True)
    if frame.empty:
        st.caption("Nothing to show.")
        return
    available = [column for column in columns if column in frame.columns]
    st.dataframe(frame[available].head(10), use_container_width=True, hide_index=True)


def render_email_crm_page(tenant: TenantContext, page: str) -> None:
    render_module_header(
        "Email CRM",
        "Work email-ready leads, outreach status, and reply state.",
    )
    normalized_page = str(page or "Generate Leads")
    if normalized_page == "Generate Leads":
        try:
            snapshot = load_dashboard_snapshot_cached()
        except Exception as error:
            st.error(str(error))
            return
        frame = load_dashboard_data(tenant)
        render_generation_status_card(snapshot)
        render_actions(tenant)
        render_dashboard_page(frame, snapshot)
        return
    if normalized_page == "Live Leads":
        frame = filtered_frame(load_dashboard_data(tenant))
        render_live_leads_page(frame)
        return
    if normalized_page in {"Email CRM", "Email"}:
        frame = load_dashboard_data(tenant)
        if not frame.empty:
            frame = frame[frame.get("verified_email", pd.Series("", index=frame.index)).astype(str).str.strip().ne("")]
        st.markdown('<div class="page-section page-card"><div class="page-card-inner dashboard-actions">', unsafe_allow_html=True)
        st.markdown('<div class="section-title">Email CRM</div>', unsafe_allow_html=True)
        st.markdown('<div class="section-caption">Gmail status, outreach summary, recent emails, replies, and simple actions.</div>', unsafe_allow_html=True)
        if frame.empty:
            render_empty_state("No email-ready leads", "Generate leads with verified email addresses to use Email CRM.", "Generate Leads")
            st.markdown("</div></div>", unsafe_allow_html=True)
            return
        preflight = load_outreach_preflight()
        sent_count = int(frame["outreach_status"].astype(str).str.lower().eq("sent").sum()) if "outreach_status" in frame.columns else 0
        failed_count = int(frame["outreach_status"].astype(str).str.lower().eq("failed").sum()) if "outreach_status" in frame.columns else 0
        replied_count = int(frame["reply_status"].astype(str).str.lower().ne("no_reply").sum()) if "reply_status" in frame.columns else 0
        no_reply_count = max(0, sent_count - replied_count)
        cards = [
            ("Gmail Connection Status", "Connected" if preflight.get("gmail_connected") else "Not connected", str(preflight.get("gmail_status_label", "") or "")),
            ("Sent", sent_count, "Outreach emails"),
            ("Failed", failed_count, "Need attention"),
            ("Replies", replied_count, "Detected replies"),
            ("No Reply", no_reply_count, "Sent without reply"),
        ]
        st.markdown('<div class="metric-card-grid">' + "".join(render_metric_card(*card) for card in cards) + "</div>", unsafe_allow_html=True)
        action_cols = st.columns(2)
        with action_cols[0]:
            if st.button("Send Outreach", use_container_width=True, disabled=not has_pro_features()):
                enqueue_job("outreach")
                st.success("Outreach job queued.")
        with action_cols[1]:
            if st.button("Check Replies", use_container_width=True, disabled=not has_pro_features()):
                enqueue_job("reply_monitor", {"mode": "once"})
                st.success("Reply check queued.")
        filter_cols = st.columns(2)
        with filter_cols[0]:
            sort_order = st.selectbox("Sort", ["Newest first", "Oldest first"], key="email_crm_sort")
        with filter_cols[1]:
            status_filter = st.selectbox("Status", ["All", "Sent", "Failed", "Replied", "No Reply"], key="email_crm_status")
        visible = email_crm_default_rows(frame, sort_order, status_filter, 10)
        recent_sent = visible[visible["outreach_status"].astype(str).str.lower().eq("sent")] if "outreach_status" in visible.columns else visible.head(0)
        recent_replies = visible[visible["reply_status"].astype(str).str.lower().ne("no_reply")] if "reply_status" in visible.columns else visible.head(0)
        failed = visible[visible["outreach_status"].astype(str).str.lower().eq("failed")] if "outreach_status" in visible.columns else visible.head(0)
        display_columns = ["company", "company_url", "verified_email", "outreach_status", "reply_status", "last_reply_at"]
        render_email_crm_table("Recent Outreach Emails", recent_sent if not recent_sent.empty else visible, display_columns)
        render_email_crm_table("Recent Replies", recent_replies, display_columns)
        render_email_crm_table("Failed Emails Needing Attention", failed, ["company", "verified_email", "outreach_error", "outreach_status"])
        with st.expander("Advanced / Debug details", expanded=False):
            st.dataframe(frame.head(50), use_container_width=True, hide_index=True)
        st.markdown("</div></div>", unsafe_allow_html=True)
        return
    if normalized_page == "Outreach":
        render_pro_action_page(
            "Outreach",
            "Queue personalized outreach for qualified leads with a connected Gmail account.",
            "Send Outreach - Pro",
            "outreach",
        )
        with st.expander("Advanced: Followups", expanded=False):
            render_pro_action_page(
                "Followups",
                "Run scheduled follow-up messages for leads that have not replied yet.",
                "Run Followups - Pro",
                "followup",
            )
            frame = load_dashboard_data(tenant)
            render_table_page(
                frame,
                ["company_url", "country", "industry", "outreach_status", "followup_count", "reply_status", "last_reply_at"],
                show_csv_export=False,
            )
        return
    if normalized_page == "Replies":
        render_pro_action_page(
            "Replies",
            "Check Gmail replies and update lead statuses.",
            "Check Replies - Pro",
            "reply_monitor",
            {"mode": "once"},
        )
        frame = load_dashboard_data(tenant)
        render_table_page(frame, ["company_url", "verified_email", "country", "reply_status", "last_reply_at"])
        return
    st.info("Choose an Email CRM tool from the sidebar.")


def render_dashboard_home(tenant: TenantContext) -> None:
    render_module_header(
        "Dashboard",
        "A simple overview of leads, outreach, replies, and active jobs.",
    )
    try:
        snapshot = load_dashboard_snapshot_cached()
    except Exception as error:
        st.error(str(error))
        return
    frame = load_dashboard_data(tenant)
    render_generation_status_card(snapshot)
    render_dashboard_page(frame, snapshot)
    st.markdown('<div class="page-section page-card"><div class="page-card-inner">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Main Actions</div>', unsafe_allow_html=True)
    st.markdown(
        "Use the sidebar for the five core workflows: generate leads, view leads, send outreach, view replies, or generate a campaign."
    )
    st.markdown("</div></div>", unsafe_allow_html=True)


def current_marketing_campaign() -> Dict[str, Any]:
    campaign = st.session_state.get("marketing_campaign_kit", {}) or {}
    return campaign if isinstance(campaign, dict) else {}


def remember_marketing_campaign(campaign: Dict[str, Any], source: str) -> None:
    st.session_state["marketing_campaign_kit"] = dict(campaign or {})
    st.session_state["marketing_campaign_source"] = source


def render_marketing_empty_state() -> None:
    render_empty_state(
        "No campaign generated yet",
        "Generate a campaign from an idea or lead first. Your latest campaign will appear here.",
        "Campaign Generator",
    )


def render_marketing_campaign_output(campaign: Dict[str, Any]) -> None:
    if not campaign:
        render_marketing_empty_state()
        return
    source = str(st.session_state.get("marketing_campaign_source", "") or "").strip()
    if source:
        st.caption(f"Latest campaign: {source}")
    st.markdown('<div class="page-section page-card"><div class="page-card-inner">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Campaign Strategy</div>', unsafe_allow_html=True)
    st.markdown(f"**Goal:** {campaign.get('campaign_goal', '')}")
    st.markdown(f"**Business idea:** {campaign.get('business_idea', '')}")
    st.markdown(f"**Target audience:** {campaign.get('target_audience', '')}")
    platforms = ", ".join(campaign.get("recommended_platforms", []) or [])
    st.markdown(f"**Recommended platforms:** {platforms}")

    budget = campaign.get("budget_plan", {}) or {}
    if budget:
        cols = st.columns(3)
        cols[0].markdown(f"**Starter**\n\n{budget.get('starter', '')}")
        cols[1].markdown(f"**Growth**\n\n{budget.get('growth', '')}")
        cols[2].markdown(f"**Agency**\n\n{budget.get('agency', '')}")

    st.markdown("### Facebook/Instagram Ads")
    for index, ad in enumerate(campaign.get("facebook_instagram_ads", []) or [], start=1):
        with st.expander(f"Ad {index}", expanded=index == 1):
            st.write(f"Primary text: {ad.get('primary_text', '')}")
            st.write(f"Headline: {ad.get('headline', '')}")
            st.write(f"Description: {ad.get('description', '')}")
            st.write(f"CTA: {ad.get('cta', '')}")

    st.markdown("### Google Search Ads")
    for index, ad in enumerate(campaign.get("google_search_ads", []) or [], start=1):
        with st.expander(f"Search ad {index}", expanded=index == 1):
            st.write(f"Headline 1: {ad.get('headline_1', '')}")
            st.write(f"Headline 2: {ad.get('headline_2', '')}")
            st.write(f"Headline 3: {ad.get('headline_3', '')}")
            st.write(f"Description 1: {ad.get('description_1', '')}")
            st.write(f"Description 2: {ad.get('description_2', '')}")

    script = campaign.get("reels_tiktok_script", {}) or {}
    if script:
        st.markdown("### Reels Script")
        st.markdown(f"**Hook:** {script.get('hook', '')}")
        st.code(str(script.get("script", "")), language=None)
        st.markdown(f"**CTA:** {script.get('cta', '')}")

    landing = campaign.get("landing_page_copy", {}) or {}
    if landing:
        st.markdown("### Landing Page Copy")
        st.write(f"Headline: {landing.get('headline', '')}")
        st.write(f"Subheadline: {landing.get('subheadline', '')}")
        for item in landing.get("bullets", []) or []:
            st.write(f"- {item}")
        st.write(f"CTA: {landing.get('cta', '')}")

    calendar = campaign.get("seven_day_content_calendar", []) or []
    if calendar:
        st.markdown("### 7-Day Content Calendar")
        st.dataframe(pd.DataFrame(calendar), use_container_width=True, hide_index=True)

    st.markdown(f"**Lead magnet:** {campaign.get('lead_magnet', '')}")
    st.markdown(f"**Next action:** {campaign.get('next_action', '')}")
    st.markdown("</div></div>", unsafe_allow_html=True)


def render_marketing_ad_copy(campaign: Dict[str, Any]) -> None:
    if not campaign:
        render_marketing_empty_state()
        return
    st.markdown('<div class="page-section page-card"><div class="page-card-inner">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Facebook/Instagram Ads</div>', unsafe_allow_html=True)
    for index, ad in enumerate(campaign.get("facebook_instagram_ads", []) or [], start=1):
        st.markdown(f"**Ad {index}**")
        st.code(
            "\n".join(
                [
                    f"Primary text: {ad.get('primary_text', '')}",
                    f"Headline: {ad.get('headline', '')}",
                    f"Description: {ad.get('description', '')}",
                    f"CTA: {ad.get('cta', '')}",
                ]
            ),
            language=None,
        )
        st.divider()
    st.markdown('<div class="section-title">Google Search Ads</div>', unsafe_allow_html=True)
    for index, ad in enumerate(campaign.get("google_search_ads", []) or [], start=1):
        st.markdown(f"**Search ad {index}**")
        st.code(
            "\n".join(
                [
                    f"Headline 1: {ad.get('headline_1', '')}",
                    f"Headline 2: {ad.get('headline_2', '')}",
                    f"Headline 3: {ad.get('headline_3', '')}",
                    f"Description 1: {ad.get('description_1', '')}",
                    f"Description 2: {ad.get('description_2', '')}",
                ]
            ),
            language=None,
        )
    st.markdown("</div></div>", unsafe_allow_html=True)


def render_marketing_reels_script(campaign: Dict[str, Any]) -> None:
    if not campaign:
        render_marketing_empty_state()
        return
    script = campaign.get("reels_tiktok_script", {}) or {}
    st.markdown('<div class="page-section page-card"><div class="page-card-inner">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Reels & TikTok Script</div>', unsafe_allow_html=True)
    st.markdown(f"**Hook**\n\n{script.get('hook', '')}")
    st.markdown("**Script**")
    st.code(str(script.get("script", "")), language=None)
    st.markdown(f"**CTA**\n\n{script.get('cta', '')}")
    st.markdown("</div></div>", unsafe_allow_html=True)


def render_marketing_content_calendar(campaign: Dict[str, Any]) -> None:
    if not campaign:
        render_marketing_empty_state()
        return
    calendar = campaign.get("seven_day_content_calendar", []) or []
    st.markdown('<div class="page-section page-card"><div class="page-card-inner">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">7-Day Content Calendar</div>', unsafe_allow_html=True)
    if calendar:
        st.dataframe(pd.DataFrame(calendar), use_container_width=True, hide_index=True)
    else:
        st.info("No content calendar available in this campaign.")
    st.markdown("</div></div>", unsafe_allow_html=True)


def render_campaign_generator_page(tenant: TenantContext) -> None:
    st.markdown('<div class="page-section page-card"><div class="page-card-inner dashboard-actions">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Campaign Generator</div>', unsafe_allow_html=True)
    st.markdown('<div class="section-caption">Build one complete campaign: ads, reels, landing page copy, calendar, and optional agency plan.</div>', unsafe_allow_html=True)
    source_mode = st.radio(
        "Generate campaign from",
        ["From Business Idea", "From Existing Lead"],
        horizontal=True,
        key="campaign_generator_source_mode",
    )
    if source_mode == "From Business Idea":
        with st.form("marketing_campaign_idea_form"):
            business_idea = st.text_input("Business/service idea", value="", placeholder="e.g. immigration consultancy for Canada visas")
            col_location, col_audience = st.columns(2)
            with col_location:
                target_location = st.text_input("Target location", value="", placeholder="e.g. Dubai, UAE")
            with col_audience:
                target_audience = st.text_input("Target audience", value="", placeholder="e.g. working professionals planning to move abroad")
            campaign_goal = st.text_input("Campaign goal", value="", placeholder="e.g. book consultation calls")
            submitted = st.form_submit_button("Generate Campaign Kit", use_container_width=True)
        if submitted:
            if not business_idea.strip():
                st.error("Business/service idea is required.")
            else:
                response = api_request(
                    "POST",
                    "/marketing/campaign/from-idea",
                    json={
                        "business_idea": business_idea.strip(),
                        "target_location": target_location.strip(),
                        "target_audience": target_audience.strip(),
                        "campaign_goal": campaign_goal.strip(),
                    },
                )
                payload = parse_api_json(response)
                if response.is_success:
                    campaign = payload.get("marketing_campaign_kit", {})
                    remember_marketing_campaign(campaign, business_idea.strip())
                    st.success("Marketing Campaign Kit generated.")
                else:
                    st.error(str(payload.get("detail", "Could not generate campaign.")))
    else:
        frame = load_dashboard_data(tenant)
        labels = lead_picker_options(frame)
        if not labels:
            st.info("No leads yet. Generate leads first, then return here to create a campaign from a lead.")
        else:
            selected_id = st.selectbox("Choose a lead", list(labels.keys()), key="campaign_generator_lead_id", format_func=lambda item: labels.get(item, item))
            if st.button("Generate Marketing Kit for Lead", use_container_width=True):
                response = api_request("POST", f"/marketing/campaign/from-lead/{selected_id}", json={})
                payload = parse_api_json(response)
                if response.is_success:
                    campaign = payload.get("marketing_campaign_kit", {})
                    remember_marketing_campaign(campaign, labels.get(selected_id, selected_id))
                    st.success("Marketing Campaign Kit generated and saved to lead metadata.")
                else:
                    st.error(str(payload.get("detail", "Could not generate campaign from lead.")))
    st.markdown("</div></div>", unsafe_allow_html=True)
    render_marketing_campaign_output(current_marketing_campaign())
    with st.expander("Advanced: Mini Agency Mode", expanded=False):
        render_mini_agency_mode_page()


def render_generate_from_lead_page(tenant: TenantContext) -> None:
    frame = load_dashboard_data(tenant)
    st.markdown('<div class="page-section page-card"><div class="page-card-inner dashboard-actions">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Generate from Lead</div>', unsafe_allow_html=True)
    st.markdown('<div class="section-caption">Create ad copy, landing page copy, scripts, and a content plan from an existing lead.</div>', unsafe_allow_html=True)
    if frame.empty:
        st.info("No leads yet. Generate or import leads first.")
        st.markdown("</div></div>", unsafe_allow_html=True)
        render_marketing_campaign_output(current_marketing_campaign())
        return
    lead_rows = [row for _, row in frame.iterrows() if str(row.get("id", "") or "").strip()]
    labels = {
        str(row.get("id")): str(row.get("company_url") or row.get("verified_email") or "Lead")[:90]
        for row in lead_rows
    }
    selected_id = st.selectbox("Choose a lead", list(labels.keys()), format_func=lambda item: labels.get(item, item))
    if st.button("Generate Marketing Kit for Lead", use_container_width=True):
        response = api_request("POST", f"/marketing/campaign/from-lead/{selected_id}", json={})
        payload = parse_api_json(response)
        if response.is_success:
            campaign = payload.get("marketing_campaign_kit", {})
            remember_marketing_campaign(campaign, labels.get(selected_id, selected_id))
            st.success("Marketing Campaign Kit generated and saved to lead metadata.")
        else:
            st.error(str(payload.get("detail", "Could not generate campaign from lead.")))
    st.markdown("</div></div>", unsafe_allow_html=True)
    render_marketing_campaign_output(current_marketing_campaign())


def current_mini_agency_plan() -> Dict[str, Any]:
    plan = st.session_state.get("mini_agency_plan", {}) or {}
    return plan if isinstance(plan, dict) else {}


def render_mini_agency_plan_output(plan: Dict[str, Any]) -> None:
    if not plan:
        render_empty_state("No mini agency plan yet", "Create a mini agency plan first. Your roadmap will appear here.", "Build Mini Agency Plan")
        return
    st.markdown('<div class="page-section page-card"><div class="page-card-inner">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Mini Agency Strategy</div>', unsafe_allow_html=True)
    st.markdown(f"**Agency positioning**\n\n{plan.get('agency_positioning', '')}")
    niches = ", ".join(plan.get("best_niches", []) or [])
    st.markdown(f"**Best niches:** {niches}")
    st.markdown(f"**Starter offer:** {plan.get('starter_offer', '')}")
    pricing = plan.get("pricing_suggestion", {}) or {}
    if pricing:
        cols = st.columns(3)
        cols[0].metric("Starter", str(pricing.get("starter", "")))
        cols[1].metric("Pro", str(pricing.get("pro", "")))
        cols[2].metric("Monthly", str(pricing.get("monthly", "")))
    queries = plan.get("lead_search_queries", []) or []
    if queries:
        st.markdown("**Lead search queries**")
        for query in queries:
            st.write(f"- {query}")
    st.markdown(f"**Next action:** {plan.get('next_action', '')}")
    st.markdown("</div></div>", unsafe_allow_html=True)

    roadmap = plan.get("daily_roadmap", []) or []
    if roadmap:
        cards = []
        for item in roadmap:
            tasks = "".join(f"<li>{html.escape(str(task))}</li>" for task in item.get("tasks", []) or [])
            cards.append(
                "<div class=\"day-card\">"
                f"<strong>Day {html.escape(str(item.get('day', '')))}: {html.escape(str(item.get('focus', '')))}</strong>"
                f"<ul>{tasks}</ul>"
                f"<p>Metric: {html.escape(str(item.get('success_metric', '')))}</p>"
                "</div>"
            )
        st.markdown(
            '<div class="page-section"><div class="section-title">14-Day Roadmap</div></div>'
            f'<div class="mini-card-grid">{"".join(cards)}</div>',
            unsafe_allow_html=True,
        )

    st.markdown('<div class="page-section page-card"><div class="page-card-inner">', unsafe_allow_html=True)
    scripts = plan.get("outreach_scripts", {}) or {}
    if scripts:
        st.markdown("**Outreach scripts**")
        st.code("\n\n".join(f"{label.title()}: {value}" for label, value in scripts.items()), language=None)
    proposal = plan.get("proposal_template", {}) or {}
    if proposal:
        st.markdown("**Proposal template**")
        for label, value in proposal.items():
            st.write(f"- {label.replace('_', ' ').title()}: {value}")
    content = plan.get("content_plan", []) or []
    if content:
        st.markdown("**Content plan**")
        for item in content:
            st.write(f"- {item.get('post', '')}: {item.get('caption', '')} CTA: {item.get('cta', '')}")
    st.markdown("</div></div>", unsafe_allow_html=True)


def render_mini_agency_mode_page() -> None:
    st.markdown('<div class="page-section page-card"><div class="page-card-inner dashboard-actions">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Build My Mini Agency Mode</div>', unsafe_allow_html=True)
    st.markdown('<div class="section-caption">Create a practical 14-day roadmap for landing your first agency clients.</div>', unsafe_allow_html=True)
    with st.form("mini_agency_plan_form"):
        cols = st.columns(2)
        with cols[0]:
            skill = st.selectbox(
                "Skill",
                ["web design", "seo", "social media", "automation", "lead generation", "marketing", "other"],
                key="mini_agency_skill",
            )
            target_country = st.text_input("Target country", value="", placeholder="e.g. UAE")
            daily_time = st.selectbox("Daily available time", ["30 minutes", "1 hour", "2 hours", "4 hours"], index=1)
        with cols[1]:
            target_city = st.text_input("Target city", value="", placeholder="e.g. Dubai")
            goal = st.selectbox("Goal", ["first client", "5 clients", "build portfolio", "sell monthly service"])
            preferred_niche = st.text_input("Preferred niche", value="", placeholder="Optional")
        submitted = st.form_submit_button("Build Mini Agency Plan", use_container_width=True)
    if submitted:
        response = api_request(
            "POST",
            "/agency/mini-agency-plan",
            json={
                "skill": skill,
                "target_country": target_country.strip(),
                "target_city": target_city.strip(),
                "daily_time": daily_time,
                "goal": goal,
                "preferred_niche": preferred_niche.strip(),
            },
        )
        payload = parse_api_json(response)
        if response.is_success:
            st.session_state["mini_agency_plan"] = dict(payload.get("mini_agency_plan", {}) or {})
            st.success("Mini agency plan generated.")
        else:
            st.error(str(payload.get("detail", "Could not generate mini agency plan.")))
    st.markdown("</div></div>", unsafe_allow_html=True)
    render_mini_agency_plan_output(current_mini_agency_plan())


def render_marketing_campaign_page(tenant: TenantContext, page: str = "Campaign Generator") -> None:
    render_module_header(
        "AI Marketing Campaign Kit",
        "Turn any lead or business idea into a complete ad campaign.",
    )
    normalized_page = str(page or "Campaign Generator")
    if normalized_page == "Campaign Generator" or normalized_page == "Generate from Business Idea":
        render_campaign_generator_page(tenant)
        return
    if normalized_page == "Generate from Lead":
        render_generate_from_lead_page(tenant)
        return
    if normalized_page == "Ad Copy":
        render_marketing_ad_copy(current_marketing_campaign())
        return
    if normalized_page == "Reels Script":
        render_marketing_reels_script(current_marketing_campaign())
        return
    if normalized_page == "7-Day Content Calendar":
        render_marketing_content_calendar(current_marketing_campaign())
        return
    if normalized_page == "Mini Agency Mode":
        render_mini_agency_mode_page()
        return
    render_campaign_generator_page(tenant)


def render_sidebar_brand() -> None:
    logo_uri = asset_data_uri(BOLT_LOGO_PATH)
    if logo_uri:
        logo_markup = f'<img class="lhai-sidebar-logo" src="{logo_uri}" alt="{html.escape(PRODUCT_NAME)} logo">'
    else:
        logo_markup = ""
    logo_block = f'<div class="lhai-sidebar-logo-wrap">{logo_markup}</div>' if logo_markup else ""
    sidebar_brand_html = (
        '<div class="lhai-sidebar-brand">'
        '<div class="lhai-sidebar-brand-inner">'
        f"{logo_block}"
        '<div class="lhai-sidebar-copy">'
        '<div class="lhai-sidebar-title">Lead Hunter AI</div>'
        '<div class="lhai-sidebar-subtitle">AI Agency Operating System</div>'
        "</div>"
        "</div>"
        "</div>"
    )
    st.sidebar.markdown(sidebar_brand_html, unsafe_allow_html=True)


def render_sidebar_navigation() -> Dict[str, str]:
    render_sidebar_brand()
    refresh_enabled = st.sidebar.checkbox("Auto-refresh", value=False)
    if refresh_enabled and st_autorefresh:
        st_autorefresh(interval=45_000, key="dashboard_refresh")

    modules = ["Dashboard", "Generate Leads", "Leads", "Email CRM", "WhatsApp CRM", "Marketing Kit", "Voice Calls"]
    if is_admin_user():
        modules.append("Admin Panel")
    account_modules = ["Billing", "Settings"]
    if st.session_state.get("sidebar_module") not in [*modules, *account_modules]:
        st.session_state["sidebar_module"] = "Dashboard"
    st.sidebar.markdown("### Modules")
    module = str(st.session_state.get("sidebar_module") or "Dashboard")
    for item in modules:
        active = item == module
        label = f"{'● ' if active else ''}{item}"
        if st.sidebar.button(label, key=f"nav_{item}", use_container_width=True, disabled=active):
            st.session_state["sidebar_module"] = item
            module = item
            st.rerun()

    st.sidebar.markdown('<div class="lhai-sidebar-account-menu">', unsafe_allow_html=True)
    auth = st.session_state.get("auth", {}) or {}
    account_label = str(auth.get("email") or auth.get("name") or "Account & Settings").strip()
    if len(account_label) > 28:
        account_label = f"{account_label[:25]}..."
    if st.sidebar.button(f"⚙️ {account_label}", key="account_settings_menu", use_container_width=True):
        st.session_state["account_menu_open"] = not bool(st.session_state.get("account_menu_open", False))
    if st.session_state.get("account_menu_open", False):
        for item in account_modules:
            active = item == module
            label = f"{'● ' if active else ''}{item}"
            if st.sidebar.button(label, key=f"account_nav_{item}", use_container_width=True, disabled=active):
                st.session_state["sidebar_module"] = item
                module = item
                st.rerun()
        if st.sidebar.button("Logout", key="account_logout", use_container_width=True):
            clear_auth_state()
            st.rerun()
    st.sidebar.markdown("</div>", unsafe_allow_html=True)

    page = module
    if module == "Marketing Kit":
        page = "Campaign Generator"
    elif module == "Settings":
        page = "Workspace Settings"
    elif module == "Admin Panel":
        page = "Admin Dashboard"
    return {"module": module, "page": page}


def render_app_footer() -> None:
    public_base_url = html.escape(API_BASE_URL, quote=True)
    st.markdown(
        f"""
        <div class="app-footer">
            Lead Hunter AI &copy; 2026 |
            <a href="{public_base_url}/privacy" target="_blank" rel="noopener">Privacy Policy</a> |
            <a href="{public_base_url}/terms" target="_blank" rel="noopener">Terms of Service</a> |
            <a href="{public_base_url}/contact" target="_blank" rel="noopener">Contact</a> |
            <a href="{public_base_url}/gmail-access" target="_blank" rel="noopener">Gmail Access</a>
        </div>
        """,
        unsafe_allow_html=True,
    )


def main() -> None:
    bootstrap_dashboard_state()
    tenant = require_login()
    if tenant is None:
        return
    render_landing_styles()
    st.markdown('<div class="app-shell">', unsafe_allow_html=True)
    navigation = render_sidebar_navigation()
    if render_plan_onboarding():
        render_app_footer()
        st.markdown("</div>", unsafe_allow_html=True)
        return

    module = navigation["module"]
    page = navigation["page"]

    if module == "Dashboard":
        render_dashboard_home(tenant)
    elif module == "Generate Leads":
        render_email_crm_page(tenant, "Generate Leads")
    elif module == "Leads":
        render_live_leads_page(load_dashboard_data(tenant))
    elif module == "Email CRM":
        render_email_crm_page(tenant, "Email CRM")
    elif module == "WhatsApp CRM":
        render_whatsapp_crm_page(tenant)
    elif module == "Marketing Kit":
        render_marketing_campaign_page(tenant, page)
    elif module == "Voice Calls":
        render_voice_calls_page(tenant)
    elif module == "Billing":
        render_module_header("Billing", "Manage plans, billing, and workspace access.")
        render_billing_page()
    elif module == "Settings":
        render_module_header("Settings", "Manage workspace integrations and automation setup.")
        render_settings_page()
    elif module == "Admin Panel":
        render_admin_page()
    else:
        render_email_crm_page(tenant, "Generate Leads")
    render_app_footer()
    st.markdown("</div>", unsafe_allow_html=True)


if __name__ == "__main__":
    main()
