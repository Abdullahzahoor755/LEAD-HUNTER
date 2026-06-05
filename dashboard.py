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
PRODUCT_TAGLINE = "Find leads, create agency pitches, and launch marketing campaigns."
LOCKED_FEATURE_MESSAGE = "This feature is available in Pro plan."

st.set_page_config(page_title=PRODUCT_NAME, page_icon="🔎", layout="wide", initial_sidebar_state="expanded")

API_BASE_URL = os.getenv("APP_API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
DEFAULT_PAYMENT_QR_PATH = "/home/mabdullah/Downloads/WhatsApp Image 2026-05-25 at 12.39.52 AM.jpeg"


def bootstrap_dashboard_state() -> None:
    st.session_state.setdefault("campaigns", [])
    st.session_state.setdefault("auth", {})
    st.session_state.setdefault("latest_subscription", {})
    st.session_state.setdefault("plan_onboarding_seen", "")
    st.session_state.setdefault("lead_generation_busy", False)
    st.session_state.setdefault("marketing_campaign_kit", {})
    st.session_state.setdefault("marketing_campaign_source", "")
    st.session_state.setdefault("offer_match", {})
    st.session_state.setdefault("offer_match_source", "")
    st.session_state.setdefault("whatsapp_sales_kit", {})
    st.session_state.setdefault("whatsapp_sales_source", "")
    st.session_state.setdefault("mini_agency_plan", {})


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
            padding-top: 2.6rem;
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
            display: block !important;
            visibility: visible !important;
            transform: none !important;
            background: #070b15;
            border-right: 1px solid rgba(148, 163, 184, .12);
            min-width: 18rem;
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
        .sidebar-brand {
            padding: .25rem .1rem 1rem;
            margin-bottom: .4rem;
            border-bottom: 1px solid rgba(148, 163, 184, .14);
        }
        .sidebar-brand-title {
            color: #f8fafc;
            font-weight: 850;
            font-size: 1.05rem;
            line-height: 1.2;
            letter-spacing: 0;
        }
        .sidebar-brand-subtitle {
            color: #94a3b8;
            font-size: .78rem;
            line-height: 1.35;
            margin-top: .18rem;
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
            .locked-grid,
            .marketing-grid {
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
                <div class="price-chip"><strong>Pro</strong>Outreach, Gmail, and reply workflow</div>
                <div class="price-chip"><strong>Agency</strong>Higher limits and agency operating tools</div>
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
        login_tab, signup_tab = st.tabs(["Login", "Create Free Account"])
        with login_tab:
            with st.form("admin_login"):
                tenant_id = st.text_input("Tenant ID", value="", placeholder="your-workspace")
                email = st.text_input("Email", value="", placeholder="you@company.com")
                password = st.text_input("Password", value="", type="password", placeholder="Enter your password")
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
                signup_tenant = st.text_input("Workspace / Tenant ID", value="", placeholder="your-workspace")
                signup_name = st.text_input("Business Name", value="", placeholder="Acme Agency")
                signup_email = st.text_input("Work Email", value="", placeholder="you@company.com")
                signup_full_name = st.text_input("Your Name", value="", placeholder="Your name")
                signup_password = st.text_input("Password", value="", type="password", placeholder="Enter your password")
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
    response = api_request("GET", "/leads?include_agency_kit=true")
    payload = response.json()
    if not response.is_success:
        raise RuntimeError(str(payload.get("detail", "Could not load leads.")))
    items = payload.get("items", [])
    rows = []
    for item in items:
        rows.append(
            {
                "id": str(item.get("id", "") or "").strip(),
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
                "agency_kit": item.get("agency_kit", {}) if isinstance(item.get("agency_kit", {}), dict) else {},
                "offer_match": item.get("offer_match", {}) if isinstance(item.get("offer_match", {}), dict) else {},
                "whatsapp_sales_kit": item.get("whatsapp_sales_kit", {}) if isinstance(item.get("whatsapp_sales_kit", {}), dict) else {},
                "marketing_campaign_kit": item.get("marketing_campaign_kit", {}) if isinstance(item.get("marketing_campaign_kit", {}), dict) else {},
            }
        )
    if not rows:
        return pd.DataFrame(columns=["id", *STANDARD_LEAD_EXPORT_COLUMNS, "agency_kit", "offer_match", "whatsapp_sales_kit", "marketing_campaign_kit"])
    frame = pd.DataFrame(rows)
    frame["score"] = pd.to_numeric(frame.get("score", pd.Series(0, index=frame.index)), errors="coerce").fillna(0)
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
                "agency_kit",
                "offer_match",
                "whatsapp_sales_kit",
                "marketing_campaign_kit",
            ]
            if column in frame.columns
        ]
    ]


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
    st.caption("This may take 1-3 minutes.")
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
                '<span class="premium-badge">Locked - Pro</span>'
                f'<h4>{html.escape(label)}</h4>'
                f'<p>{html.escape(LOCKED_FEATURE_MESSAGE)}</p>'
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
    cards = "".join(render_metric_card(label, value, hint) for label, value, hint in metrics)
    st.markdown(f'<div class="page-section"><div class="metric-card-grid">{cards}</div></div>', unsafe_allow_html=True)


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
        render_empty_state("No leads yet", "Enter a niche and country to generate your first leads.", "Generate Leads")
        st.markdown("</div></div>", unsafe_allow_html=True)
        return
    chart_left, chart_right = st.columns(2)
    with chart_left:
        st.plotly_chart(px.histogram(frame, x="score", nbins=10, title="Lead Scores"), use_container_width=True)
    with chart_right:
        st.plotly_chart(px.pie(frame, names="outreach_status", title="Pipeline"), use_container_width=True)
    st.markdown("</div></div>", unsafe_allow_html=True)


def render_table_page(frame: pd.DataFrame, columns: List[str]) -> None:
    st.markdown('<div class="page-section page-card"><div class="page-card-inner dashboard-actions">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Lead Table</div>', unsafe_allow_html=True)
    st.markdown('<div class="section-caption">Review saved leads and export the visible columns.</div>', unsafe_allow_html=True)
    visible = frame.copy()
    for column in columns:
        if column not in visible.columns:
            visible[column] = ""
    export_frame = visible[columns]
    if export_frame.empty:
        render_empty_state("No leads yet", "Enter a niche and country to generate your first leads.", "Generate Leads")
    else:
        st.caption(f"Showing {len(export_frame)} lead rows.")
        st.dataframe(export_frame, use_container_width=True, hide_index=True)
        csv_frame = sanitize_csv_frame(export_frame)
        st.download_button(
            "Export Visible Leads CSV",
            csv_frame.to_csv(index=False).encode("utf-8"),
            file_name="leads.csv",
            mime="text/csv",
            use_container_width=True,
        )
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
        '<div class="section-caption">Rule-based fallback kits. Free: 3/month, Pro: 100/month, Agency: 1000/month.</div>',
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
    render_ai_provider_settings()
    render_gmail_settings()
    render_account_plan_settings()


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
    st.markdown(
        f'<span class="status-badge {"success" if configured else "warning"}">{"Connected" if configured else "Not connected"}</span>',
        unsafe_allow_html=True,
    )
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


def render_account_plan_settings() -> None:
    auth = st.session_state.get("auth", {}) or {}
    snapshot: Dict[str, Any] = {}
    try:
        response = api_request("GET", "/dashboard/snapshot")
        body = parse_api_json(response)
        if response.is_success:
            snapshot = body
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
    render_page_header("Admin Dashboard", "Workspace analytics, users, leads, jobs, and payment approvals.", "Admin")
    summary = admin_get("/admin/summary")
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
    if st.button(button_label, use_container_width=True):
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


def render_email_crm_page(tenant: TenantContext, page: str) -> None:
    render_module_header(
        "Email CRM",
        "Find leads, manage contacts, generate agency kits, and automate outreach.",
    )
    normalized_page = str(page or "Generate Leads")
    if normalized_page == "Generate Leads":
        snapshot_response = api_request("GET", "/dashboard/snapshot")
        snapshot = snapshot_response.json()
        if not snapshot_response.is_success:
            st.error(str(snapshot.get("detail", "Could not load dashboard snapshot.")))
            return
        frame = load_dashboard_data(tenant)
        render_generation_status_card(snapshot)
        render_actions(tenant)
        render_dashboard_page(frame, snapshot)
        return
    if normalized_page == "Live Leads":
        frame = filtered_frame(load_dashboard_data(tenant))
        render_table_page(frame, STANDARD_LEAD_EXPORT_COLUMNS)
        return
    if normalized_page == "AI Agency Kit":
        frame = load_dashboard_data(tenant)
        render_agency_kit_panel(frame)
        return
    if normalized_page == "Offer Matchmaker":
        render_offer_matchmaker_page(tenant)
        return
    if normalized_page == "WhatsApp Sales Kit":
        render_whatsapp_sales_kit_page(tenant)
        return
    if normalized_page == "Outreach":
        render_pro_action_page(
            "Outreach",
            "Queue personalized outreach for qualified leads with a connected Gmail account.",
            "Send Outreach - Pro",
            "outreach",
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
    if normalized_page == "Followups":
        render_pro_action_page(
            "Followups",
            "Run scheduled follow-up messages for leads that have not replied yet.",
            "Run Followups - Pro",
            "followup",
        )
        frame = load_dashboard_data(tenant)
        render_table_page(frame, ["company_url", "country", "industry", "outreach_status", "followup_count", "reply_status", "last_reply_at"])
        return
    if normalized_page == "CSV Export":
        frame = load_dashboard_data(tenant)
        render_table_page(frame, STANDARD_LEAD_EXPORT_COLUMNS)
        return
    st.info("Choose an Email CRM tool from the sidebar.")


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
        st.markdown("### 7-Day Calendar")
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


def render_campaign_generator_page() -> None:
    st.markdown('<div class="page-section page-card"><div class="page-card-inner dashboard-actions">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Campaign Generator</div>', unsafe_allow_html=True)
    st.markdown('<div class="section-caption">Build a complete fallback campaign from any business or service idea.</div>', unsafe_allow_html=True)
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
    st.markdown("</div></div>", unsafe_allow_html=True)
    render_marketing_campaign_output(current_marketing_campaign())


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
        render_campaign_generator_page()
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
    render_campaign_generator_page()


def render_sidebar_navigation() -> Dict[str, str]:
    st.sidebar.markdown(
        """
        <div class="sidebar-brand">
            <div class="sidebar-brand-title">Lead Hunter AI</div>
            <div class="sidebar-brand-subtitle">AI Agency Operating System</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.sidebar.markdown("### Workspace")
    if st.sidebar.button("Logout", use_container_width=True):
        st.session_state["auth"] = {}
        st.session_state["latest_subscription"] = {}
        st.session_state["plan_onboarding_seen"] = ""
        st.rerun()
    refresh_enabled = st.sidebar.checkbox("Auto-refresh", value=True)
    if refresh_enabled and st_autorefresh:
        st_autorefresh(interval=30_000, key="dashboard_refresh")

    modules = ["Email CRM", "Marketing Kit", "Settings"]
    if is_admin_user():
        modules.append("Admin")
    if st.session_state.get("sidebar_module") not in modules:
        st.session_state["sidebar_module"] = "Email CRM"
    st.sidebar.markdown("### Modules")
    module = st.sidebar.radio("Module", modules, key="sidebar_module", label_visibility="collapsed")

    page = ""
    if module == "Email CRM":
        crm_pages = [
            "Generate Leads",
            "Live Leads",
            "AI Agency Kit",
            "Offer Matchmaker",
            "WhatsApp Sales Kit",
            "Outreach",
            "Replies",
            "Followups",
            "CSV Export",
        ]
        if st.session_state.get("email_crm_page") not in crm_pages:
            st.session_state["email_crm_page"] = "Generate Leads"
        st.sidebar.markdown("### Email CRM")
        page = st.sidebar.radio("Email CRM", crm_pages, key="email_crm_page", label_visibility="collapsed")
    elif module == "Marketing Kit":
        marketing_pages = [
            "Campaign Generator",
            "Generate from Lead",
            "Ad Copy",
            "Reels Script",
            "7-Day Content Calendar",
            "Mini Agency Mode",
        ]
        if st.session_state.get("marketing_kit_page") not in marketing_pages:
            st.session_state["marketing_kit_page"] = "Campaign Generator"
        st.sidebar.markdown("### Marketing Kit")
        page = st.sidebar.radio("Marketing Kit", marketing_pages, key="marketing_kit_page", label_visibility="collapsed")
    elif module == "Settings":
        settings_pages = ["Workspace Settings", "Billing & Plan"]
        if st.session_state.get("settings_page") not in settings_pages:
            st.session_state["settings_page"] = "Workspace Settings"
        st.sidebar.markdown("### Settings")
        page = st.sidebar.radio("Settings", settings_pages, key="settings_page", label_visibility="collapsed")
    elif module == "Admin":
        page = "Admin Dashboard"
    return {"module": module, "page": page}


def main() -> None:
    bootstrap_dashboard_state()
    tenant = require_login()
    if tenant is None:
        return
    render_landing_styles()
    st.markdown('<div class="app-shell">', unsafe_allow_html=True)
    navigation = render_sidebar_navigation()
    if render_plan_onboarding():
        st.markdown("</div>", unsafe_allow_html=True)
        return

    module = navigation["module"]
    page = navigation["page"]

    if module == "Email CRM":
        render_email_crm_page(tenant, page)
    elif module == "Marketing Kit":
        render_marketing_campaign_page(tenant, page)
    elif module == "Settings":
        if page == "Billing & Plan":
            render_module_header("Settings", "Manage plans, billing, and workspace preferences.")
            render_billing_page()
        else:
            render_module_header("Settings", "Manage workspace integrations and automation setup.")
            render_settings_page()
    elif module == "Admin":
        render_admin_page()
    else:
        render_email_crm_page(tenant, "Generate Leads")
    st.markdown("</div>", unsafe_allow_html=True)


if __name__ == "__main__":
    main()
