"""
Shared runtime for the AI Sales Assistant.

This module keeps the original lead generation and outreach pipeline while
adding shared helpers for Gmail replies, follow-ups, and dashboard views.
"""

import base64
import asyncio
import json
import logging
import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import unquote, urljoin, urlparse

import httpx
import requests
from anthropic import Anthropic
from app.agents.lead_pipeline import CleaningAgent, DiscoveryAgent, OutreachAgent, ScoringAgent, ScraperAgent, run_agent
from app.core.models import Lead, TenantContext
from app.db.session import get_async_db_session, reset_async_session_factory
from app.models.sqlalchemy import TenantRecord
from app.services.auth_service import AuthService
from app.services.lead_service import LeadService
from app.services.outreach_service import OutreachService
from app.services._async import maybe_await
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from sqlalchemy import select

try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
except ImportError:
    Request = None
    Credentials = None
    InstalledAppFlow = None
    build = None
    HttpError = Exception


SERPER_SEARCH_URL = "https://google.serper.dev/search"
DEFAULT_CLAUDE_MODEL = "claude-sonnet-4-6"
DEFAULT_LEAD_LIMIT = 10
MIN_QUALIFIED_LEAD_SCORE = 50
DEFAULT_SHEET_RANGE = "Sheet1!A1"
LOCAL_TIMEZONE = timezone.utc
UTC = timezone.utc
REQUEST_CONNECT_TIMEOUT = 5
REQUEST_READ_TIMEOUT = 20
SEARCH_TIMEOUT = 8
MAX_RETRIES_PER_REQUEST = 3
MAX_PAGES_PER_DOMAIN = 8
MAX_WORKERS = 4
MAX_HTML_BYTES = 250_000
FIT_SCORE_EMAIL = 40
FIT_SCORE_PHONE = 25
FIT_SCORE_RELEVANCE = 20
FIT_SCORE_QUALITY = 10
MIN_FALLBACK_SCORE = 10
SCRAPE_FAILURE_REASONS = {
    "FAILED_HTTP2",
    "FAILED_TIMEOUT",
    "FAILED_BLOCKED",
    "FAILED_EMPTY_CONTENT",
    "FAILED_REQUEST_ERROR",
    "FAILED_UNKNOWN",
}

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
]
PROMPT_FILES = {
    "skill": "skill.md",
    "spec": "spec.md",
    "claude": "claude.md",
}

EXCLUDED_DOMAINS = (
    "github.com",
    "linkedin.com",
    "facebook.com",
    "instagram.com",
    "youtube.com",
    "twitter.com",
    "x.com",
    "yelp.com",
    "yellowpages.com",
    "crunchbase.com",
    "clutch.co",
    "designrush.com",
    "glassdoor.com",
    "glassdoor.",
    "indeed.com",
    "lusha.com",
    "rozee.pk",
    "quora.com",
    "reddit.com",
    "scribd.com",
    "techbehemoths.com",
    "elioplus.com",
    "f6s.com",
    "bayt.com",
    "naukrigulf.com",
    "vocal.media",
    "zoominfo.com",
    "forbesmiddleeast.com",
    "companiesmarketcap.com",
    "businessmagnet.co.uk",
    "wikipedia.org",
    "maps.google.",
    "google.com",
    "gov.pk",
    ".gov",
    ".gov.",
    ".gov/",
)

DIRECTORY_KEYWORDS = (
    "/directory",
    "/directories",
    "/listing",
    "/listings",
    "/category",
    "/categories",
    "/search",
)

EXCLUDED_RESULT_KEYWORDS = (
    "embassy",
    "consulate",
    "government",
    "ministry",
    "directory",
    "directories",
    "listing",
    "listings",
    "job",
    "jobs",
    "career",
    "careers",
    "internship",
    "internships",
    "salary",
    "salaries",
    "review",
    "reviews",
    "forum",
    "community",
    "market cap",
    "marketcap",
    "startup directory",
)

BUSINESS_RESULT_KEYWORDS = (
    "company",
    "companies",
    "agency",
    "agencies",
    "software house",
    "software development",
    "it services",
    "technology",
    "digital marketing",
    "web development",
    "business services",
    "solutions",
    "services",
)


def root_domain_from_url(url: str) -> str:
    host = get_website_key(url)
    parts = [part for part in host.split(".") if part]
    if len(parts) >= 3 and parts[-2] in {"com", "net", "org", "gov"}:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def search_result_rejection_reason(result: Dict[str, str]) -> str:
    link = str(result.get("link", "") or "").strip().lower()
    title = str(result.get("title", "") or "").strip().lower()
    snippet = str(result.get("snippet", "") or "").strip().lower()
    combined = " ".join([link, title, snippet])
    if not link:
        return "rejected_invalid_url"
    if "github.com" in link:
        return "rejected_bad_domain_github"
    if any(marker in combined for marker in ("embassy", "consulate", "government", "ministry", "gov.pk", ".gov")):
        return "rejected_government_or_embassy"
    if any(marker in combined for marker in ("jobs", "career", "careers", "internship", "rozee.pk", "indeed.com")):
        return "rejected_job_board"
    if any(marker in link for marker in ("facebook.com", "youtube.com", "linkedin.com/jobs", "instagram.com", "twitter.com", "x.com")):
        return "rejected_social_media"
    if any(marker in combined for marker in ("directory", "listing", "listings", "marketplace", "techbehemoths", "designrush", "zoominfo", "lusha")):
        return "rejected_directory"
    if not is_valid_company_website(str(result.get("link", "") or "")):
        return "rejected_invalid_url"
    if not looks_like_relevant_business_result(result):
        return "rejected_irrelevant_title"
    return ""


def lead_quality_grade(website: str, metadata: Dict[str, Any], email: str = "", phone: str = "") -> str:
    if not website or any(marker in website.lower() for marker in EXCLUDED_DOMAINS):
        return "Rejected"
    score = 0
    if root_domain_from_url(website):
        score += 3
    combined = " ".join(
        [
            str(metadata.get("industry", "")),
            str(metadata.get("company_summary", "")),
            str(metadata.get("reason", "")),
            str(metadata.get("quality_reason", "")),
        ]
    ).lower()
    if any(keyword in combined for keyword in BUSINESS_RESULT_KEYWORDS):
        score += 2
    if str(metadata.get("contact_page", "") or "").strip():
        score += 2
    if str(email or "").strip():
        score += 2
    if str(phone or "").strip():
        score += 1
    if score >= 8 and str(email or "").strip():
        return "A"
    if score >= 5:
        return "B"
    return "C"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/118.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0",
]
SCRAPE_REFERERS = [
    "https://www.google.com/",
    "https://www.google.com.pk/",
    "https://www.bing.com/",
]

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
CREDENTIALS_FILE = BASE_DIR / "credentials.json"
TOKEN_FILE = BASE_DIR / "token.json"

CONTACT_PATH_KEYWORDS = (
    "contact",
    "about",
    "company",
    "team",
    "staff",
    "people",
    "careers",
    "career",
    "jobs",
    "support",
    "customer-service",
    "locations",
    "reach-us",
    "get-in-touch",
)
EMAIL_PATTERN = re.compile(r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[A-Za-z]{2,}\b")
PHONE_PATTERN = re.compile(r"(?:(?:\+|00)\d{1,3}[\s().-]*)?(?:\d[\s().-]*){7,}\d")
ADDRESS_HINTS = ("street", "road", "avenue", "tower", "building", "office", "dubai", "riyadh", "london")
COMMON_CONTACT_PATHS = (
    "/",
    "/home",
    "/contact",
    "/contact-us",
    "/contacts",
    "/about",
    "/about-us",
    "/team",
    "/our-team",
    "/company",
    "/careers",
    "/career",
    "/jobs",
    "/support",
    "/customer-service",
    "/reach-us",
    "/get-in-touch",
)
COMMON_BUSINESS_EMAIL_PREFIXES = ("info", "sales", "contact", "hello", "support")
EMAIL_BAD_SUBSTRINGS = (
    "example.com",
    "email.com",
    "yourname@",
    "your-name@",
    "domain.com",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".svg",
    "sentry.io",
)

GENERATED_LEADS: List[Dict[str, Any]] = []
LAST_PIPELINE_STATS: Dict[str, Any] = {}
LAST_PIPELINE_EVENTS: List[Dict[str, Any]] = []
GMAIL_PROFILE_EMAIL: Optional[str] = None
UNSUBSCRIBE_FOOTER = "\n\nIf you prefer not to hear from us again, reply with unsubscribe."

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
LOGGER = logging.getLogger("ai-sales-assistant")


class CrawlContext:
    def __init__(self) -> None:
        self.visited_urls: Set[str] = set()
        self.visited_domains: Set[str] = set()
        self.page_count_by_domain: Dict[str, int] = {}
        self.last_status: str = ""
        self.last_method: str = ""
        self.last_reason: str = ""


RELEVANT_BUSINESS_KEYWORDS = (
    "b2b",
    "enterprise",
    "logistics",
    "manufacturing",
    "industrial",
    "construction",
    "supply chain",
    "distribution",
    "services",
    "solutions",
    "automation",
    "technology",
    "infrastructure",
    "cloud",
    "cybersecurity",
    "systems integration",
    "software",
)

LOW_QUALITY_PAGE_KEYWORDS = (
    "coming soon",
    "under construction",
    "access denied",
    "forbidden",
    "just a moment",
    "enable javascript",
    "not found",
)

INTENT_SIGNAL_KEYWORDS = {
    "buying_intent": (
        "contact us",
        "request a quote",
        "book a demo",
        "get started",
        "schedule a call",
        "talk to sales",
        "services",
        "solutions",
    ),
    "commercial_presence": (
        "about us",
        "our clients",
        "case study",
        "industries we serve",
        "why choose us",
        "portfolio",
        "projects",
    ),
    "operations": (
        "head office",
        "locations",
        "phone",
        "email",
        "support",
        "contact",
        "company",
    ),
}

LISTING_SIGNAL_KEYWORDS = (
    "business directory",
    "directory",
    "list of companies",
    "top companies",
    "best companies",
    "browse companies",
    "classifieds",
    "marketplace listing",
    "company database",
)

REAL_BUSINESS_SIGNAL_KEYWORDS = (
    "our services",
    "about us",
    "our team",
    "contact us",
    "industries",
    "solutions",
    "clients",
    "projects",
    "company profile",
)


def load_text_file(file_path: str) -> str:
    """Safely load a text file and return its contents."""
    resolved_path = BASE_DIR / file_path
    try:
        with resolved_path.open("r", encoding="utf-8") as file:
            return file.read().strip()
    except FileNotFoundError:
        LOGGER.warning("Prompt file not found: %s", resolved_path)
        return ""
    except OSError as error:
        LOGGER.error("Could not read %s: %s", resolved_path, error)
        return ""


def load_skill_prompt() -> str:
    return load_text_file(PROMPT_FILES["skill"])


def load_spec_prompt() -> str:
    return load_text_file(PROMPT_FILES["spec"])


def load_claude_prompt() -> str:
    return load_text_file(PROMPT_FILES["claude"])


def get_candidate_models() -> List[str]:
    configured_model = os.getenv("ANTHROPIC_MODEL", "").strip()
    candidates = [configured_model, DEFAULT_CLAUDE_MODEL, "claude-haiku-4-5-20251001"]
    unique_candidates: List[str] = []
    seen = set()
    for model in candidates:
        if model and model not in seen:
            seen.add(model)
            unique_candidates.append(model)
    return unique_candidates


def load_environment() -> None:
    if not ENV_FILE.exists():
        LOGGER.warning(".env file not found at: %s", ENV_FILE)
        return
    if ENV_FILE.stat().st_size == 0:
        LOGGER.warning(".env file is empty: %s", ENV_FILE)
        return
    load_dotenv(dotenv_path=ENV_FILE, override=False)


def now_utc() -> datetime:
    return datetime.now(UTC)


def to_iso8601(value: Optional[datetime] = None) -> str:
    return (value or now_utc()).replace(microsecond=0).isoformat()


def parse_datetime(value: str) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        normalized = raw.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except ValueError:
        return None


def is_email_required_for_leads() -> bool:
    return True


def is_google_sheets_enabled() -> bool:
    return False


def get_sheet_id() -> str:
    return ""


def get_sheet_range() -> str:
    return os.getenv("GOOGLE_SHEET_RANGE", DEFAULT_SHEET_RANGE).strip() or DEFAULT_SHEET_RANGE


def retry_operation(operation: Callable, description: str, attempts: int = 3, delay_seconds: int = 2):
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except Exception as error:
            last_error = error
            LOGGER.warning("%s failed (attempt %s/%s): %s", description, attempt, attempts, error)
            if attempt < attempts:
                time.sleep(delay_seconds)
    raise last_error


def build_search_query(niche: str = "", location: str = "", query: str = "") -> str:
    niche_value = str(niche or "").strip()
    location_value = str(location or "").strip()
    query_value = re.sub(r"\s+", " ", str(query or "").strip())
    if niche_value and location_value:
        lowered = niche_value.lower()
        if lowered in {"it", "i.t", "information technology"}:
            return f"IT companies in {location_value}"
        if "software" in lowered:
            return f"software houses in {location_value}"
        if "digital marketing" in lowered:
            return f"digital marketing agencies in {location_value}"
        return f"{niche_value} companies in {location_value}"
    return query_value


def build_search_query_variants(niche: str = "", location: str = "", query: str = "") -> List[str]:
    final_query = build_search_query(niche=niche, location=location, query=query)
    location_value = str(location or "").strip()
    niche_value = str(niche or "").strip().lower()
    variants: List[str] = []
    if location_value and niche_value in {"it", "i.t", "information technology"}:
        variants = [
            f"IT companies in {location_value}",
            f"software houses in {location_value}",
            f"software development companies in {location_value}",
            f"IT services companies in {location_value}",
            f"technology companies in {location_value}",
            f"custom software development companies in {location_value}",
        ]
    elif final_query:
        variants = [final_query]
    deduped: List[str] = []
    for item in variants:
        normalized = re.sub(r"\s+", " ", str(item or "").strip())
        if normalized and normalized.lower() not in {existing.lower() for existing in deduped}:
            deduped.append(normalized)
    return deduped


def search_google(query: str, num: int = 10) -> List[Dict[str, str]]:
    api_key = os.getenv("SERPER_API_KEY", "").strip()
    if not api_key:
        LOGGER.error("SERPER_API_KEY is missing.")
        return []

    payload = {"q": query, "num": max(10, min(int(num or 10), 100))}
    headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}

    LOGGER.info("Searching Google for: %s", query)
    try:
        response = requests.post(
            SERPER_SEARCH_URL,
            headers=headers,
            json=payload,
            timeout=(REQUEST_CONNECT_TIMEOUT, SEARCH_TIMEOUT),
        )
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as error:
        LOGGER.error("Search failed for '%s': %s", query, error)
        return []
    except ValueError:
        LOGGER.error("Invalid JSON from Serper for '%s'", query)
        return []

    results = []
    for item in data.get("organic", [])[:40]:
        results.append(
            {"title": item.get("title", ""), "link": item.get("link", ""), "snippet": item.get("snippet", "")}
        )
    LOGGER.info("Found %s organic results for: %s", len(results), query)
    return results


def is_valid_company_website(url: str) -> bool:
    if not url or "<" in url or ">" in url:
        return False
    lowered = url.lower()
    if any(domain in lowered for domain in EXCLUDED_DOMAINS):
        return False
    if any(keyword in lowered for keyword in DIRECTORY_KEYWORDS):
        return False
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def looks_like_directory_result(result: Dict[str, str]) -> bool:
    combined_text = " ".join(
        [result.get("title", "").strip(), result.get("snippet", "").strip(), result.get("link", "").strip()]
    ).lower()
    return any(keyword in combined_text for keyword in EXCLUDED_RESULT_KEYWORDS)


def looks_like_relevant_business_result(result: Dict[str, str]) -> bool:
    combined_text = " ".join(
        [result.get("title", "").strip(), result.get("snippet", "").strip(), result.get("link", "").strip()]
    ).lower()
    if any(keyword in combined_text for keyword in BUSINESS_RESULT_KEYWORDS):
        return True
    link = result.get("link", "").strip().lower()
    parsed = urlparse(link if "://" in link else f"https://{link}")
    path = parsed.path.strip("/")
    return bool(parsed.netloc and not path)


def normalize_homepage_url(url: str) -> str:
    parsed = urlparse(url)
    scheme = parsed.scheme or "https"
    netloc = parsed.netloc.lower()
    return f"{scheme}://{netloc}"


def normalize_crawl_url(url: str) -> str:
    parsed = urlparse(url)
    scheme = parsed.scheme or "https"
    netloc = parsed.netloc.lower()
    path = parsed.path.rstrip("/") or "/"
    return f"{scheme}://{netloc}{path}"


def is_valid_crawl_url(url: str) -> bool:
    if not url or "<" in url or ">" in url:
        return False
    parsed = urlparse(url if "://" in url else f"https://{url}")
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def get_website_key(url: str) -> str:
    if not url:
        return ""
    if "://" not in url:
        url = f"https://{url}"
    parsed = urlparse(url)
    netloc = parsed.netloc.lower().strip()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc.rstrip("/")


def extract_websites_with_stats(search_results: List[Dict[str, str]]) -> Tuple[List[str], Dict[str, int]]:
    websites = []
    seen = set()
    stats = {
        "raw_results_count": len(search_results),
        "filtered_results_count": 0,
        "rejected_bad_domain_count": 0,
        "rejected_irrelevant_count": 0,
        "duplicate_domain_count": 0,
    }
    events: List[Dict[str, Any]] = []
    for result in search_results:
        url = result.get("link", "").strip()
        domain = root_domain_from_url(url)
        title = str(result.get("title", "") or "").strip()[:160]
        events.append({"stage": "searching", "status": "found", "domain": domain, "url": url, "title": title, "message": "raw_result_found"})
        reason = search_result_rejection_reason(result)
        if reason:
            if reason in {"rejected_bad_domain_github", "rejected_government_or_embassy", "rejected_job_board", "rejected_social_media", "rejected_invalid_url"}:
                stats["rejected_bad_domain_count"] += 1
            else:
                stats["rejected_irrelevant_count"] += 1
            events.append({"stage": "filtering", "status": "rejected", "domain": domain, "url": url, "title": title, "reason": reason, "message": "raw_result_rejected"})
            continue
        homepage = normalize_homepage_url(url)
        website_key = root_domain_from_url(homepage)
        if website_key in seen:
            stats["duplicate_domain_count"] += 1
            events.append({"stage": "filtering", "status": "rejected", "domain": website_key, "url": homepage, "title": title, "reason": "rejected_duplicate_domain", "message": "duplicate_domain_skipped"})
            continue
        seen.add(website_key)
        websites.append(homepage)
        stats["filtered_results_count"] += 1
        events.append({"stage": "filtering", "status": "accepted", "domain": website_key, "url": homepage, "title": title, "message": "domain_accepted"})
    LOGGER.info("Extracted %s unique company websites.", len(websites))
    stats["events"] = events  # type: ignore[assignment]
    return websites, stats


def extract_websites(search_results: List[Dict[str, str]]) -> List[str]:
    websites, _ = extract_websites_with_stats(search_results)
    return websites


def clean_visible_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(("script", "style", "noscript", "svg")):
        tag.decompose()
    text = soup.get_text(separator=" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def should_skip_url(url: str) -> bool:
    lowered = url.lower()
    blocked_extensions = (
        ".pdf",
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".svg",
        ".webp",
        ".zip",
        ".rar",
        ".mp4",
        ".mp3",
    )
    irrelevant_segments = ("/blog", "/news", "/careers", "/jobs", "/privacy", "/terms", "/cookie")
    return lowered.endswith(blocked_extensions) or any(segment in lowered for segment in irrelevant_segments)


def build_scrape_headers() -> Dict[str, str]:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Ch-Ua": '"Chromium";v="120", "Google Chrome";v="120"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Referer": "https://www.google.com/",
    }


def _set_scrape_failure(context: Optional[CrawlContext], status: str, reason: str, method: str) -> None:
    if context is None:
        return
    context.last_status = status
    context.last_reason = reason
    context.last_method = method


def build_scrape_result(
    *,
    content: Optional[str],
    status: str,
    failure_reason: str,
    method_used: str,
) -> Dict[str, Any]:
    return {
        "content": content,
        "status": status,
        "failure_reason": failure_reason,
        "method_used": method_used,
    }


def extract_title_meta_jsonld(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    parts: List[str] = []
    if soup.title and soup.title.get_text(" ", strip=True):
        parts.append(soup.title.get_text(" ", strip=True))

    meta_description = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    if meta_description and meta_description.get("content"):
        parts.append(str(meta_description.get("content", "")).strip())

    og_description = soup.find("meta", attrs={"property": re.compile("^og:description$", re.I)})
    if og_description and og_description.get("content"):
        parts.append(str(og_description.get("content", "")).strip())

    for script in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        script_text = script.get_text(" ", strip=True)
        if script_text:
            parts.append(script_text[:1000])
    return re.sub(r"\s+", " ", " ".join(part for part in parts if part)).strip()


def extract_readable_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(("script", "style", "noscript", "svg", "nav", "header", "form")):
        tag.decompose()
    candidates = soup.find_all(["main", "article", "section", "div"])
    text_blocks: List[str] = []
    for candidate in candidates:
        text = candidate.get_text(" ", strip=True)
        if len(text) >= 120:
            text_blocks.append(text)
        if len(" ".join(text_blocks)) >= 3000:
            break
    if text_blocks:
        return re.sub(r"\s+", " ", " ".join(text_blocks)).strip()[:3000]
    return ""


def fetch_page(url: str, context: Optional[CrawlContext] = None) -> Dict[str, Any]:
    if not is_valid_crawl_url(url):
        LOGGER.info("Skipping malformed URL: %s", url)
        _set_scrape_failure(context, "request_failed", "FAILED_UNKNOWN:malformed url skipped", "url_validation")
        return build_scrape_result(
            content=None,
            status="FAILED",
            failure_reason="FAILED_UNKNOWN",
            method_used="url_validation",
        )

    normalized_url = normalize_crawl_url(url)
    domain_key = get_website_key(normalized_url)

    if should_skip_url(normalized_url):
        LOGGER.info("Skipping irrelevant page: %s", normalized_url)
        if context is not None:
            context.last_status = "irrelevant_page"
            context.last_method = "url_filter"
            context.last_reason = "irrelevant extension or segment"
        return build_scrape_result(
            content=None,
            status="FAILED",
            failure_reason="FAILED_EMPTY_CONTENT",
            method_used="url_filter",
        )

    if context is not None:
        if normalized_url in context.visited_urls:
            LOGGER.info("Skipping already visited URL: %s", normalized_url)
            return build_scrape_result(
                content=None,
                status="FAILED",
                failure_reason="FAILED_EMPTY_CONTENT",
                method_used="url_validation",
            )
        if context.page_count_by_domain.get(domain_key, 0) >= MAX_PAGES_PER_DOMAIN:
            LOGGER.info("Skipping %s because domain page limit reached.", normalized_url)
            return build_scrape_result(
                content=None,
                status="FAILED",
                failure_reason="FAILED_EMPTY_CONTENT",
                method_used="url_validation",
            )
        context.visited_urls.add(normalized_url)
        context.page_count_by_domain[domain_key] = context.page_count_by_domain.get(domain_key, 0) + 1

    timeout = httpx.Timeout(connect=REQUEST_CONNECT_TIMEOUT, read=REQUEST_READ_TIMEOUT, write=REQUEST_READ_TIMEOUT, pool=REQUEST_READ_TIMEOUT)
    for attempt in range(MAX_RETRIES_PER_REQUEST + 1):
        headers = build_scrape_headers()
        if attempt:
            headers["Referer"] = random.choice(SCRAPE_REFERERS)
            time.sleep(random.uniform(1.5, 3.5))
        started_at = time.perf_counter()
        http2_failed = False
        try:
            LOGGER.info("Visiting URL: %s (attempt %s)", normalized_url, attempt + 1)
            response = None
            method_used = "httpx_http2"
            try:
                with httpx.Client(
                    headers=headers,
                    timeout=timeout,
                    follow_redirects=True,
                    http2=True,
                    verify=False,
                ) as client:
                    response = client.get(normalized_url)
            except Exception as client_err:
                http2_failed = True
                LOGGER.warning("HTTPX HTTP2 failed for %s: %s", normalized_url, client_err)
                method_used = "httpx_http1"
                try:
                    with httpx.Client(
                        headers=headers,
                        timeout=timeout,
                        follow_redirects=True,
                        http2=False,
                        verify=False,
                    ) as client:
                        response = client.get(normalized_url)
                except Exception as fallback_err:
                    LOGGER.warning("HTTPX HTTP1 failed for %s: %s", normalized_url, fallback_err)
                    raise fallback_err

            elapsed = time.perf_counter() - started_at
            LOGGER.info("Response time %.2fs for %s using %s", elapsed, normalized_url, method_used)
            LOGGER.info("HTTP status %s for %s using %s", response.status_code, normalized_url, method_used)
            if elapsed > REQUEST_READ_TIMEOUT and context is not None:
                context.last_status = "slow_site"
                context.last_method = method_used
                context.last_reason = f"response slower than {REQUEST_READ_TIMEOUT}s"
            response.raise_for_status()

            content_type = response.headers.get("Content-Type", "").lower()
            content_length = int(response.headers.get("Content-Length", "0") or "0")
            if "text/html" not in content_type:
                LOGGER.info("Skipping non-HTML page: %s (%s)", normalized_url, content_type or "unknown")
                if context is not None:
                    context.last_status = "non_html"
                    context.last_method = method_used
                    context.last_reason = f"content-type={content_type or 'unknown'}"
                response.close()
                return build_scrape_result(
                    content=None,
                    status="FAILED",
                    failure_reason="FAILED_EMPTY_CONTENT",
                    method_used=method_used,
                )
            if content_length and content_length > MAX_HTML_BYTES:
                LOGGER.info("Skipping heavy page: %s (%s bytes)", normalized_url, content_length)
                if context is not None:
                    context.last_status = "heavy_page"
                    context.last_method = method_used
                    context.last_reason = f"content-length={content_length}"
                response.close()
                return build_scrape_result(
                    content=None,
                    status="FAILED",
                    failure_reason="FAILED_EMPTY_CONTENT",
                    method_used=method_used,
                )

            if not response.encoding:
                response.encoding = "utf-8"
            if context is not None:
                context.last_status = "ok"
                context.last_method = method_used
                context.last_reason = "html fetched"
            return build_scrape_result(
                content=response.text,
                status="SUCCESS",
                failure_reason="",
                method_used=method_used,
            )
        except httpx.HTTPError as error:
            elapsed = time.perf_counter() - started_at
            LOGGER.error("Scrape failed for %s: %s: %s", normalized_url, type(error).__name__, str(error))
            LOGGER.warning("Fetch failed for %s after %.2fs via %s: %s", normalized_url, elapsed, method_used, error)
            status_code = getattr(getattr(error, "response", None), "status_code", None)
            if status_code in (401, 403):
                _set_scrape_failure(context, "blocked_site", f"FAILED_BLOCKED:http {status_code}", method_used)
            elif elapsed >= REQUEST_READ_TIMEOUT or isinstance(error, httpx.TimeoutException):
                _set_scrape_failure(context, "slow_site", f"FAILED_TIMEOUT:request timeout after {elapsed:.2f}s", method_used)
            else:
                _set_scrape_failure(context, "request_failed", f"FAILED_REQUEST_ERROR:{error}", method_used)
            if attempt >= MAX_RETRIES_PER_REQUEST:
                LOGGER.info("Skip reason: blocked/slow/error page %s", normalized_url)
                return build_scrape_result(
                    content=None,
                    status="FAILED",
                    failure_reason=(
                        "FAILED_HTTP2"
                        if http2_failed and status_code not in (401, 403) and not isinstance(error, httpx.TimeoutException)
                        else "FAILED_BLOCKED"
                        if status_code in (401, 403)
                        else "FAILED_TIMEOUT"
                        if elapsed >= REQUEST_READ_TIMEOUT or isinstance(error, httpx.TimeoutException)
                        else "FAILED_REQUEST_ERROR"
                    ),
                    method_used=method_used,
                )
            if status_code == 403:
                time.sleep(5 * (attempt + 1))
            else:
                time.sleep(2**attempt)
    _set_scrape_failure(context, "request_failed", "FAILED_UNKNOWN:exhausted retries", "httpx")
    return build_scrape_result(
        content=None,
        status="FAILED",
        failure_reason="FAILED_UNKNOWN",
        method_used="httpx",
    )


def scrape_website(url: str, context: Optional[CrawlContext] = None) -> Tuple[str, str, str]:
    result = fetch_page(url, context=context)
    LOGGER.info(
        "Scrape result for %s: status=%s method=%s failure_reason=%s",
        url,
        result.get("status", ""),
        result.get("method_used", ""),
        result.get("failure_reason", ""),
    )
    if result.get("status") != "SUCCESS":
        return "", str(result.get("method_used", "httpx")), str(result.get("failure_reason", "FAILED_UNKNOWN"))
    html = str(result.get("content", "") or "")[:MAX_HTML_BYTES]
    if any(keyword in html.lower() for keyword in LOW_QUALITY_PAGE_KEYWORDS):
        LOGGER.info("Skipping low-quality or blocked page content: %s", url)
        _set_scrape_failure(context, "js_site", "FAILED_EMPTY_CONTENT:blocked or placeholder content", "httpx")
        return "", "httpx", "FAILED_EMPTY_CONTENT:blocked or placeholder content"

    primary_text = clean_visible_text(html)[:3000]
    if primary_text:
        return primary_text, "httpx_bs4", ""

    readable_text = extract_readable_text(html)
    if readable_text:
        if context is not None:
            context.last_method = "readability_fallback"
            context.last_reason = "bs4 text empty; readability fallback used"
        return readable_text, "readability_fallback", "bs4 text empty"

    fallback_text = extract_title_meta_jsonld(html)
    if fallback_text:
        if context is not None:
            context.last_method = "meta_title_jsonld_fallback"
            context.last_reason = "content extracted from title/meta/json-ld"
        return fallback_text[:3000], "meta_title_jsonld_fallback", "bs4 and readability empty"

    _set_scrape_failure(context, "js_site", "FAILED_EMPTY_CONTENT:no usable text after all fallback methods", "fallback_failed")
    return "", "fallback_failed", "FAILED_EMPTY_CONTENT:no usable text after all fallback methods"


def extract_company_name(html: str, website: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    for separator in ("|", "-", "•"):
        if separator in title:
            title = title.split(separator)[0].strip()
            break
    if title:
        return title[:120]
    for heading_tag in ("h1", "h2"):
        heading = soup.find(heading_tag)
        if heading:
            return heading.get_text(" ", strip=True)[:120]
    return urlparse(website).netloc.replace("www.", "")


def extract_contact_links(html: str, base_url: str) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    links = []
    seen = set()
    base_key = get_website_key(base_url)

    for anchor in soup.find_all("a", href=True):
        href = (anchor.get("href") or "").strip()
        label = anchor.get_text(" ", strip=True).lower()
        if not href:
            continue
        absolute_url = urljoin(base_url, href)
        if not is_valid_crawl_url(absolute_url):
            continue
        parsed = urlparse(absolute_url)
        if parsed.scheme not in ("http", "https"):
            continue
        if get_website_key(absolute_url) != base_key:
            continue
        cleaned = absolute_url.strip()
        path = parsed.path.lower()
        if not any(keyword in path or keyword in label for keyword in CONTACT_PATH_KEYWORDS):
            continue
        if cleaned not in seen:
            seen.add(cleaned)
            links.append(cleaned)
    return links


def build_common_contact_urls(base_url: str) -> List[str]:
    parsed = urlparse(base_url)
    root = f"{parsed.scheme}://{parsed.netloc}"
    return [urljoin(root, path) for path in COMMON_CONTACT_PATHS]


def deobfuscate_emails(text: str) -> str:
    replacements = (
        (r"\s*\[\s*at\s*\]\s*", "@"),
        (r"\s*\(\s*at\s*\)\s*", "@"),
        (r"\s+\bat\b\s+", "@"),
        (r"\s*\[\s*dot\s*\]\s*", "."),
        (r"\s*\(\s*dot\s*\)\s*", "."),
        (r"\s+\bdot\b\s+", "."),
    )
    deobfuscated = text
    for pattern, replacement in replacements:
        deobfuscated = re.sub(pattern, replacement, deobfuscated, flags=re.IGNORECASE)
    return deobfuscated


def is_valid_email(email: str) -> bool:
    email = email.lower().strip(" .,:;()[]<>")
    if not email:
        return False
    if any(bad in email for bad in EMAIL_BAD_SUBSTRINGS):
        return False
    if email.startswith(("noreply@", "no-reply@", "donotreply@", "do-not-reply@")):
        return False
    return bool(EMAIL_PATTERN.fullmatch(email))


def _root_domain_from_domain(domain: str) -> str:
    cleaned = str(domain or "").lower().strip().strip(".")
    if cleaned.startswith("www."):
        cleaned = cleaned[4:]
    parts = [part for part in cleaned.split(".") if part]
    if len(parts) < 2:
        return ""
    compound_suffixes = {
        "com.au", "com.sa", "co.uk", "com.pk", "com.br", "com.tr", "com.sg", "co.in", "co.za", "com.my"
    }
    suffix = ".".join(parts[-2:])
    if suffix in compound_suffixes and len(parts) >= 3:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _email_domain(email: str) -> str:
    return str(email or "").lower().split("@", 1)[-1].strip()


def _append_email_candidate(candidates: List[Dict[str, str]], seen: Set[str], email: str, source: str, page_url: str = "") -> None:
    normalized = str(email or "").lower().strip(" .,:;()[]<>")
    if not is_valid_email(normalized) or normalized in seen:
        return
    seen.add(normalized)
    candidates.append(
        {
            "email": normalized,
            "source": str(source or "page_text"),
            "page_url": str(page_url or ""),
            "confidence": "verified_email",
        }
    )


def _structured_values(payload: Any, parent_key: str = "") -> Tuple[List[str], List[str]]:
    emails: List[str] = []
    phones: List[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            lowered_key = str(key or "").lower()
            nested_emails, nested_phones = _structured_values(value, lowered_key)
            emails.extend(nested_emails)
            phones.extend(nested_phones)
    elif isinstance(payload, list):
        for item in payload:
            nested_emails, nested_phones = _structured_values(item, parent_key)
            emails.extend(nested_emails)
            phones.extend(nested_phones)
    elif isinstance(payload, str):
        if "email" in parent_key:
            emails.extend(EMAIL_PATTERN.findall(deobfuscate_emails(payload)))
        if parent_key in {"telephone", "phone", "contactphone", "faxnumber"}:
            phones.append(payload)
    return emails, phones


def extract_structured_contact_info(html: str) -> Dict[str, List[str]]:
    soup = BeautifulSoup(html, "html.parser")
    emails: List[str] = []
    phones: List[str] = []
    seen_emails: Set[str] = set()
    seen_phones: Set[str] = set()

    for script in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.IGNORECASE)}):
        raw = script.string or script.get_text(" ", strip=True)
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        structured_emails, structured_phones = _structured_values(parsed)
        for email in structured_emails:
            normalized = str(email or "").lower().strip(" .,:;()[]<>")
            if is_valid_email(normalized) and normalized not in seen_emails:
                seen_emails.add(normalized)
                emails.append(normalized)
        for phone in structured_phones:
            normalized_phone = normalize_phone(phone)
            if normalized_phone and normalized_phone not in seen_phones:
                seen_phones.add(normalized_phone)
                phones.append(normalized_phone)

    for node in soup.select("[itemprop='email'], [property='email']"):
        for value in (node.get("content", ""), node.get("href", ""), node.get_text(" ", strip=True)):
            for email in EMAIL_PATTERN.findall(deobfuscate_emails(str(value or ""))):
                normalized = email.lower().strip(" .,:;()[]<>")
                if is_valid_email(normalized) and normalized not in seen_emails:
                    seen_emails.add(normalized)
                    emails.append(normalized)

    for node in soup.select("[itemprop='telephone'], [property='telephone'], [itemprop='phone'], [property='phone']"):
        for value in (node.get("content", ""), node.get("href", ""), node.get_text(" ", strip=True)):
            normalized_phone = normalize_phone(str(value or ""))
            if normalized_phone and normalized_phone not in seen_phones:
                seen_phones.add(normalized_phone)
                phones.append(normalized_phone)

    return {"emails": emails, "phones": phones}


def extract_email_candidates_from_page(page_url: str, html: str) -> List[Dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    candidates: List[Dict[str, str]] = []
    seen: Set[str] = set()

    for anchor in soup.find_all("a", href=True):
        href = (anchor.get("href") or "").strip()
        if href.lower().startswith("mailto:"):
            email = unquote(href.split(":", 1)[1].split("?", 1)[0]).strip(" .,:;()[]<>")
            _append_email_candidate(candidates, seen, email, "mailto", page_url)

    structured = extract_structured_contact_info(html)
    for email in structured.get("emails", []):
        _append_email_candidate(candidates, seen, email, "structured_data", page_url)

    footer_texts = []
    for selector in ("footer", ".footer", "#footer", "[class*='footer']", "[id*='footer']"):
        for node in soup.select(selector):
            footer_texts.append(node.get_text(" ", strip=True))
    for candidate in EMAIL_PATTERN.findall(deobfuscate_emails(" ".join(footer_texts))):
        _append_email_candidate(candidates, seen, candidate, "footer", page_url)

    searchable_text = " ".join(
        [
            clean_visible_text(html),
            deobfuscate_emails(soup.get_text(" ", strip=True)),
            deobfuscate_emails(html),
        ]
    )
    for candidate in EMAIL_PATTERN.findall(searchable_text):
        _append_email_candidate(candidates, seen, candidate, "page_text", page_url)
    return candidates


def extract_email_candidates_from_html_pages(html_pages: List[Tuple[str, str]]) -> List[Dict[str, str]]:
    candidates: List[Dict[str, str]] = []
    seen: Set[str] = set()
    for page_url, html in html_pages:
        for candidate in extract_email_candidates_from_page(page_url, html):
            email = candidate["email"]
            if email in seen:
                continue
            seen.add(email)
            candidates.append(candidate)
    return candidates


def extract_emails_from_html(html: str) -> List[str]:
    return [candidate["email"] for candidate in extract_email_candidates_from_page("", html)]


def extract_emails_from_page_html(html: str) -> List[str]:
    return extract_emails_from_html(html)


def choose_best_email_candidate(html_pages: List[Tuple[str, str]], candidates: List[Dict[str, str]]) -> str:
    role_priority = ("sales@", "hello@", "info@", "contact@", "support@", "enquiry@", "inquiry@")
    source_priority = {"mailto": 0, "structured_data": 1, "footer": 2, "page_text": 3}
    page_roots = {
        _root_domain_from_domain(urlparse(page_url).netloc)
        for page_url, _ in html_pages
        if _root_domain_from_domain(urlparse(page_url).netloc)
    }

    def rank(candidate: Dict[str, str]) -> Tuple[int, int, int, str]:
        email = candidate["email"]
        same_domain = _root_domain_from_domain(_email_domain(email)) in page_roots
        role_index = next((index for index, prefix in enumerate(role_priority) if email.startswith(prefix)), len(role_priority))
        role_score = 0 if role_index < len(role_priority) else 1
        domain_score = 0 if same_domain else 2
        source_score = source_priority.get(str(candidate.get("source", "")), 9)
        return (domain_score, role_score, role_index, f"{source_score}:{email}")

    return sorted(candidates, key=rank)[0]["email"] if candidates else ""


def extract_first_email_from_html_pages(html_pages: List[Tuple[str, str]]) -> str:
    candidates = extract_email_candidates_from_html_pages(html_pages)
    return choose_best_email_candidate(html_pages, candidates)


def build_likely_business_emails(website: str, observed_email: str) -> List[str]:
    if observed_email:
        return []
    parsed = urlparse(website if "://" in website else f"https://{website}")
    domain = parsed.netloc.lower().strip()
    if domain.startswith("www."):
        domain = domain[4:]
    if not domain or "." not in domain or any(excluded in domain for excluded in EXCLUDED_DOMAINS):
        return []
    return [f"{prefix}@{domain}" for prefix in COMMON_BUSINESS_EMAIL_PREFIXES]


def build_likely_business_email(website: str, observed_email: str) -> str:
    likely_emails = build_likely_business_emails(website, observed_email)
    return likely_emails[0] if likely_emails else ""


def contact_readiness_score(verified_email: str, likely_email: str, phone: str) -> int:
    if str(verified_email or "").strip():
        return 100
    if str(likely_email or "").strip():
        return 70
    if str(phone or "").strip():
        return 40
    return 0


def extract_first_email(text: str) -> str:
    for email in EMAIL_PATTERN.findall(deobfuscate_emails(text)):
        lowered = email.lower()
        if is_valid_email(lowered):
            return lowered
    return ""


def normalize_phone(phone: str) -> str:
    cleaned = re.sub(r"\s+", " ", phone).strip(" -.,;")
    digits = re.sub(r"\D", "", cleaned)
    if len(digits) < 7 or len(digits) > 15:
        return ""
    if re.fullmatch(r"20\d{2}[-/.\s]\d{1,2}[-/.\s]\d{1,2}", cleaned):
        return ""
    if re.fullmatch(r"\d{1,2}[-/.\s]\d{1,2}[-/.\s]20\d{2}", cleaned):
        return ""
    if re.fullmatch(r"\d{4}[-/.\s]\d{2}[-/.\s]\d{2}", cleaned):
        return ""
    return cleaned


def extract_first_phone(text: str) -> str:
    for match in PHONE_PATTERN.findall(text):
        phone = normalize_phone(match)
        if phone:
            return phone
    return ""


def extract_address(text: str) -> str:
    snippets = re.split(r"(?<=[.!?])\s+", text)
    for snippet in snippets:
        lowered = snippet.lower()
        if any(hint in lowered for hint in ADDRESS_HINTS):
            return snippet.strip()[:200]
    return ""


def extract_contact_info(website: str, context: Optional[CrawlContext] = None) -> Dict[str, str]:
    homepage_result = fetch_page(website, context=context)
    LOGGER.info(
        "Contact extraction homepage result for %s: status=%s method=%s failure_reason=%s",
        website,
        homepage_result.get("status", ""),
        homepage_result.get("method_used", ""),
        homepage_result.get("failure_reason", ""),
    )
    if homepage_result.get("status") != "SUCCESS":
        return {
            "company_name": "",
            "email": "",
            "phone": "",
            "address": "",
            "contact_page": "",
            "failure_reason": str(homepage_result.get("failure_reason", "FAILED_EMPTY_CONTENT")),
        }

    homepage_html = str(homepage_result.get("content", "") or "")
    html_pages = [(website.rstrip("/"), homepage_html)]
    contact_links = extract_contact_links(homepage_html, website) or build_common_contact_urls(website)
    extra_paths = [
        "/contact",
        "/contact-us",
        "/about",
        "/about-us",
        "/team",
        "/our-team",
        "/careers",
        "/career",
        "/jobs",
        "/support",
        "/company",
    ]
    contact_links = list(dict.fromkeys(contact_links + [urljoin(website.rstrip("/") + "/", path.lstrip("/")) for path in extra_paths]))
    seen_links = {website.rstrip("/")}
    for link in contact_links:
        normalized_link = link.rstrip("/")
        if normalized_link in seen_links:
            continue
        if not is_valid_crawl_url(normalized_link):
            continue
        seen_links.add(normalized_link)
        response = fetch_page(link, context=context)
        LOGGER.info(
            "Contact page result for %s: status=%s method=%s failure_reason=%s",
            normalized_link,
            response.get("status", ""),
            response.get("method_used", ""),
            response.get("failure_reason", ""),
        )
        if response.get("status") == "SUCCESS":
            html_pages.append((normalized_link, str(response.get("content", "") or "")))
        if len(html_pages) >= MAX_PAGES_PER_DOMAIN:
            break

    combined_text_parts = []
    contact_page = ""
    for page_url, html in html_pages:
        combined_text_parts.append(clean_visible_text(html))
        if page_url.rstrip("/") != website.rstrip("/") and not contact_page:
            contact_page = page_url

    combined_text = " ".join(combined_text_parts)
    email_candidates = extract_email_candidates_from_html_pages(html_pages)
    email = choose_best_email_candidate(html_pages, email_candidates) or extract_first_email(combined_text)
    structured_phones: List[str] = []
    for _, html in html_pages:
        structured_phones.extend(extract_structured_contact_info(html).get("phones", []))
    phone = structured_phones[0] if structured_phones else extract_first_phone(combined_text)
    likely_emails = build_likely_business_emails(website, email)
    likely_email = likely_emails[0] if likely_emails else ""
    failure_reason = "FAILED_NO_EMAIL" if not email else ""
    if context is not None and not email:
        context.last_status = "no_email"
        context.last_reason = failure_reason
        context.last_method = "contact_probe"
    return {
        "company_name": extract_company_name(homepage_html, website),
        "email": email,
        "phone": phone,
        "address": extract_address(combined_text),
        "contact_page": contact_page,
        "failure_reason": failure_reason,
        "email_confidence": "verified_email" if email else ("likely_email" if likely_email else "unknown"),
        "email_candidates": email_candidates,
        "candidate_emails": [candidate["email"] for candidate in email_candidates],
        "likely_email": likely_email,
        "likely_emails": likely_emails,
        "lead_readiness_score": contact_readiness_score(email, likely_email, phone),
    }


def compute_business_relevance_score(analysis: Dict[str, Any], website_text: str) -> Tuple[int, str]:
    score = 0
    reason_parts: List[str] = []
    lowered_text = website_text.lower()
    keyword_hits = sum(1 for keyword in RELEVANT_BUSINESS_KEYWORDS if keyword in lowered_text)

    if bool(analysis.get("needs_it_services")):
        score += 10
        reason_parts.append("AI marked the business as relevant")
    if keyword_hits:
        keyword_score = min(10, keyword_hits * 2)
        score += keyword_score
        reason_parts.append(f"{keyword_hits} relevance keyword hit(s)")

    industry = str(analysis.get("industry", "")).strip()
    if industry:
        reason_parts.append(f"industry={industry}")

    intent_analysis = analysis.get("intent_analysis", {}) if isinstance(analysis.get("intent_analysis", {}), dict) else {}
    buying_intent_score = int(intent_analysis.get("buying_intent_score", 0) or 0)
    service_demand_score = int(intent_analysis.get("service_demand_score", 0) or 0)
    urgency_score = int(intent_analysis.get("urgency_score", 0) or 0)
    llm_signal_bonus = min(10, (buying_intent_score + service_demand_score + urgency_score) // 30)
    if llm_signal_bonus:
        score += llm_signal_bonus
        reason_parts.append(
            f"llm_intent={buying_intent_score}/100 service_demand={service_demand_score}/100 urgency={urgency_score}/100"
        )

    return min(FIT_SCORE_RELEVANCE, score), ", ".join(reason_parts) or "limited business relevance signals"


def compute_website_quality_score(website: str, website_text: str, company_name: str, contact_info: Dict[str, str]) -> Tuple[int, str]:
    score = 0
    reasons: List[str] = []
    text_length = len(website_text)

    if company_name:
        score += 4
        reasons.append("company name found")
    if contact_info.get("contact_page"):
        score += 3
        reasons.append("contact page found")
    if text_length >= 600:
        score += 2
        reasons.append("enough page content")
    elif text_length >= 250:
        score += 1
        reasons.append("basic page content")
    if "https://" in website.lower():
        score += 1
        reasons.append("https enabled")

    return min(FIT_SCORE_QUALITY, score), ", ".join(reasons) or "thin or low-quality website"


def detect_domain_type(website: str) -> Tuple[str, str]:
    normalized = website.strip().lower()
    if not normalized:
        return "unknown", "missing website"
    netloc = get_website_key(normalized)
    if any(domain in netloc for domain in EXCLUDED_DOMAINS):
        return "excluded", "excluded platform/domain"
    if any(keyword in normalized for keyword in DIRECTORY_KEYWORDS):
        return "listing", "directory/listing path detected"
    suffix = netloc.rsplit(".", 1)[-1] if "." in netloc else ""
    if suffix in {"gov", "edu"}:
        return "non_business", f"{suffix} domain type"
    if suffix in {"org"}:
        return "neutral", ".org domain requires stronger business evidence"
    return "business", "commercial domain pattern"


def extract_intent_signals(website_text: str) -> Dict[str, Any]:
    lowered_text = website_text.lower()
    matched_signals: List[str] = []
    category_counts: Dict[str, int] = {}
    total_hits = 0
    for category, keywords in INTENT_SIGNAL_KEYWORDS.items():
        hits = [keyword for keyword in keywords if keyword in lowered_text]
        if not hits:
            continue
        category_counts[category] = len(hits)
        total_hits += len(hits)
        matched_signals.extend(hits[:3])
    return {
        "signal_count": total_hits,
        "signals": matched_signals[:8],
        "categories": category_counts,
    }


def build_human_readable_save_reason(
    *,
    domain_type: str,
    real_business_signal_count: int,
    intent_signal_count: int,
    email: str,
    phone: str,
    company_name: str,
    website_text: str,
) -> str:
    email_present = bool(str(email or "").strip())
    email_verified = email_present and is_valid_email(email)
    phone_present = bool(str(phone or "").strip())

    if intent_signal_count >= 6 and email_verified:
        return "High-intent company with verified contact - strong outreach candidate."
    if intent_signal_count >= 3 and email_present:
        return "Moderate buying intent detected with available contact - worth reaching out."
    if not email_present and phone_present:
        return "No email found but phone number available - consider WhatsApp outreach."
    if intent_signal_count < 3:
        return "Low intent signals - saved for reference only."
    return "Moderate buying intent detected - worth reviewing before outreach."


def blocklisted_lead_reason(lead: Dict[str, Any], query: str) -> str:
    website = str(lead.get("website", "") or lead.get("Website", "") or "").strip()
    company_name = str(lead.get("company_name", "") or lead.get("Company", "") or "").strip()
    industry = str(lead.get("industry", "") or lead.get("Industry", "") or "").strip().lower()
    parsed_host = urlparse(website if "://" in website else f"https://{website}").netloc.lower()
    root_domain = root_domain_from_url(website)
    domain_parts = [part for part in parsed_host.replace("www.", "").split(".") if part]
    root_parts = [part for part in root_domain.split(".") if part]
    blocked_suffixes = {"gov", "edu", "mil", "int"}
    if any(part in blocked_suffixes for part in [*domain_parts, *root_parts]):
        return "blocked_domain_extension"

    combined = f"{company_name} {website}".lower()
    blocked_keywords = (
        "embassy",
        "consulate",
        "ambassador",
        "professor",
        "prof.",
        "dr.",
        "personal",
        "university",
        "college",
        "school",
        "institute",
        "magazine",
        "journal",
        "newsletter",
        "blog",
        "wikipedia",
        "facebook",
        "linkedin",
        "twitter",
        "instagram",
    )
    if any(keyword in combined for keyword in blocked_keywords):
        return "blocked_entity_keyword"

    query_has_it_intent = bool(re.search(r"\b(it|software|automation|ai)\b", str(query or "").lower()))
    industry_mismatch = industry in {"retail", "fashion", "food"}
    if query_has_it_intent and industry_mismatch:
        return "blocked_industry_mismatch"
    return ""


def apply_lead_quality_filter(website: str, website_text: str, company_name: str, contact_info: Dict[str, str]) -> Dict[str, Any]:
    lowered_text = website_text.lower()
    reasons: List[str] = []
    score = 0

    is_directory = any(keyword in lowered_text for keyword in LISTING_SIGNAL_KEYWORDS) or any(
        keyword in website.lower() for keyword in DIRECTORY_KEYWORDS
    )
    if is_directory:
        reasons.append("directory/listing signals detected")
        score -= 45

    domain_type, domain_reason = detect_domain_type(website)
    reasons.append(domain_reason)
    if domain_type == "business":
        score += 20
    elif domain_type == "neutral":
        score += 5
    elif domain_type == "listing":
        score -= 30
    elif domain_type in {"excluded", "non_business"}:
        score -= 35

    real_business_hits = [keyword for keyword in REAL_BUSINESS_SIGNAL_KEYWORDS if keyword in lowered_text]
    if company_name:
        score += 8
        reasons.append("company name present")
    if contact_info.get("email"):
        score += 10
        reasons.append("contact email found")
    if contact_info.get("phone"):
        score += 8
        reasons.append("phone found")
    if real_business_hits:
        score += min(20, len(real_business_hits) * 4)
        reasons.append(f"real business signals={len(real_business_hits)}")
    else:
        reasons.append("limited real business signals")

    intent_signals = extract_intent_signals(website_text)
    signal_count = int(intent_signals["signal_count"])
    if signal_count:
        score += min(24, signal_count * 4)
        reasons.append(f"intent signals={signal_count}")
    else:
        reasons.append("no strong intent signals")

    if len(website_text.strip()) >= 500:
        score += 10
        reasons.append("sufficient website text")
    elif len(website_text.strip()) >= 200:
        score += 5
        reasons.append("basic website text")
    else:
        reasons.append("thin website text")

    final_score = max(0, min(100, score))
    if is_directory or domain_type in {"excluded", "listing", "non_business"}:
        category = "cold" if final_score < 70 else "warm"
    elif final_score >= 75:
        category = "hot"
    elif final_score >= 45:
        category = "warm"
    else:
        category = "cold"

    return {
        "score": final_score,
        "reason": build_human_readable_save_reason(
            domain_type=domain_type,
            real_business_signal_count=len(real_business_hits),
            intent_signal_count=signal_count,
            email=str(contact_info.get("email", "") or "").strip(),
            phone=str(contact_info.get("phone", "") or "").strip(),
            company_name=company_name,
            website_text=website_text,
        ),
        "category": category,
        "is_directory": is_directory,
        "domain_type": domain_type,
        "intent_signals": intent_signals,
        "real_business_signal_count": len(real_business_hits),
    }


def score_lead(
    website: str,
    contact_info: Dict[str, str],
    analysis: Dict[str, Any],
    website_text: str,
    lead_status: str = "",
) -> Dict[str, Any]:
    email = contact_info.get("email", "")
    phone = contact_info.get("phone", "")

    email_score = FIT_SCORE_EMAIL if is_valid_email(email) else 0
    phone_score = FIT_SCORE_PHONE if phone else 0
    relevance_score, relevance_reason = compute_business_relevance_score(analysis, website_text)
    quality_score, quality_reason = compute_website_quality_score(
        website=website,
        website_text=website_text,
        company_name=contact_info.get("company_name", ""),
        contact_info=contact_info,
    )
    total_score = min(100, email_score + phone_score + relevance_score + quality_score)
    if not website_text.strip():
        total_score = max(total_score, MIN_FALLBACK_SCORE)

    score_reasons = [
        f"email={email_score}/{FIT_SCORE_EMAIL}",
        f"phone={phone_score}/{FIT_SCORE_PHONE}",
        f"relevance={relevance_score}/{FIT_SCORE_RELEVANCE} ({relevance_reason})",
        f"quality={quality_score}/{FIT_SCORE_QUALITY} ({quality_reason})",
    ]
    if not website_text.strip():
        score_reasons.append(f"minimum_fallback={MIN_FALLBACK_SCORE}/100 due to missing text")
    if lead_status:
        score_reasons.append(f"status={lead_status}")

    if not email_score:
        score_reasons.append("missing email -> marked no_email")

    return {
        "lead_score": total_score,
        "score_breakdown": " | ".join(score_reasons),
        "email_status": "Pending" if email_score else "no_email",
    }


def extract_json_from_text(text: str) -> Optional[Dict[str, Any]]:
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    fenced_match = re.search(r"```json\s*(\{.*?\})\s*```", stripped, re.DOTALL)
    if fenced_match:
        try:
            return json.loads(fenced_match.group(1))
        except json.JSONDecodeError:
            pass

    object_match = re.search(r"(\{.*\})", stripped, re.DOTALL)
    if object_match:
        try:
            return json.loads(object_match.group(1))
        except json.JSONDecodeError:
            return None
    return None


def run_claude_json(system_prompt: str, user_prompt: str, max_tokens: int = 700, temperature: float = 0) -> Dict[str, Any]:
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY is missing.")

    client = Anthropic(api_key=api_key)
    last_error = None
    for model_name in get_candidate_models():
        try:
            message = client.messages.create(
                model=model_name,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
            response_text = "\n".join(
                getattr(block, "text", "") for block in message.content if getattr(block, "text", "")
            )
            parsed = extract_json_from_text(response_text)
            if parsed:
                return parsed
        except Exception as error:
            last_error = error
            LOGGER.warning("Claude model failed (%s): %s", model_name, error)
    raise RuntimeError(f"Claude request failed. Last error: {last_error}")


def analyze_lead_with_claude(website_text: str) -> Dict[str, Any]:
    if not website_text.strip():
        LOGGER.warning("Empty website text received for Claude analysis.")
        return {}

    skill_prompt = load_skill_prompt()
    spec_prompt = load_spec_prompt()
    claude_prompt = load_claude_prompt()
    system_prompt = "\n\n".join(part for part in [skill_prompt, spec_prompt, claude_prompt] if part) or (
        "You are an AI lead qualification assistant for a B2B IT system integrator. "
        "Analyze company website content and return only valid JSON."
    )
    user_prompt = (
        "Analyze the following company website content and decide whether the company is a good B2B lead "
        "for IT services such as cloud, infrastructure, cybersecurity, managed services, software "
        "integration, or enterprise automation.\n\n"
        "If the website belongs to a directory, publisher, forum, social platform, government entity, "
        "job board, or content aggregator instead of an operating company, mark needs_it_services as false "
        "and give a low lead_score.\n\n"
        "Also detect buying intent, service demand, and urgency from the website wording. "
        "Score each on a 0-100 scale and summarize why.\n\n"
        "Crucially, look for any email address mentioned in the text. If you find one, return it in 'extracted_email'.\n\n"
        "Return only valid JSON in this exact structure:\n"
        '{\n  "company_summary": "",\n  "industry": "",\n  "needs_it_services": true,\n  "extracted_email": "",\n'
        '  "lead_score": 1,\n  "reason": "",\n'
        '  "intent_analysis": {\n'
        '    "buying_intent_score": 0,\n'
        '    "service_demand_score": 0,\n'
        '    "urgency_score": 0,\n'
        '    "intent_summary": "",\n'
        '    "signals": []\n'
        "  }\n}\n\n"
        f"Website content:\n{website_text}\n"
    )
    try:
        parsed = run_claude_json(system_prompt, user_prompt, max_tokens=900, temperature=0)
    except Exception as error:
        LOGGER.error("Claude qualification failed: %s", error)
        return {}

    intent_analysis = parsed.get("intent_analysis", {})
    if not isinstance(intent_analysis, dict):
        intent_analysis = {}
    normalized_intent_analysis = {
        "buying_intent_score": int(intent_analysis.get("buying_intent_score", 0) or 0),
        "service_demand_score": int(intent_analysis.get("service_demand_score", 0) or 0),
        "urgency_score": int(intent_analysis.get("urgency_score", 0) or 0),
        "intent_summary": str(intent_analysis.get("intent_summary", "")).strip(),
        "signals": [str(signal).strip() for signal in intent_analysis.get("signals", []) if str(signal).strip()],
    }
    parsed["intent_analysis"] = normalized_intent_analysis
    return parsed


def has_required_email(lead: Dict[str, Any]) -> bool:
    email = str(lead.get("email", lead.get("Email", ""))).strip().lower()
    return is_valid_email(email)


def get_google_credentials():
    if not all([Credentials, Request, InstalledAppFlow]):
        raise RuntimeError(
            "Google API packages are missing. Install google-api-python-client, "
            "google-auth-httplib2, and google-auth-oauthlib."
        )

    credentials = None
    if TOKEN_FILE.exists():
        try:
            credentials = Credentials.from_authorized_user_file(str(TOKEN_FILE), GOOGLE_SCOPES)
        except Exception as error:
            LOGGER.warning("Could not read token.json, recreating it: %s", error)
            TOKEN_FILE.unlink(missing_ok=True)

    if credentials and credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
        except Exception as error:
            LOGGER.warning("Google token refresh failed, recreating token.json: %s", error)
            TOKEN_FILE.unlink(missing_ok=True)
            credentials = None

    if not credentials or not credentials.valid:
        if not CREDENTIALS_FILE.exists():
            raise FileNotFoundError(f"Missing Google OAuth file: {CREDENTIALS_FILE}")
        flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), GOOGLE_SCOPES)
        credentials = flow.run_local_server(port=0)
        with TOKEN_FILE.open("w", encoding="utf-8") as file:
            file.write(credentials.to_json())
    return credentials


def get_google_service(api_name: str, version: str):
    if not build:
        raise RuntimeError("google-api-python-client is missing. Install the Google API dependencies.")
    return build(api_name, version, credentials=get_google_credentials())


def get_gmail_service():
    return get_google_service("gmail", "v1")


def get_sheets_service():
    return get_google_service("sheets", "v4")


def get_gmail_profile_email() -> str:
    global GMAIL_PROFILE_EMAIL
    if GMAIL_PROFILE_EMAIL:
        return GMAIL_PROFILE_EMAIL
    try:
        profile = get_gmail_service().users().getProfile(userId="me").execute()
        GMAIL_PROFILE_EMAIL = str(profile.get("emailAddress", "")).strip().lower()
    except Exception as error:
        LOGGER.warning("Could not load Gmail profile email: %s", error)
        GMAIL_PROFILE_EMAIL = ""
    return GMAIL_PROFILE_EMAIL


def generate_cold_email(lead: Dict[str, str]) -> Tuple[str, str]:
    company = str(lead.get("Company", "")).strip()
    website = str(lead.get("Website", "")).strip()
    reason = str(lead.get("Reason", "")).strip()
    prompt = (
        "Write a short personalized cold email.\n\n"
        f"- Mention company name: {company}\n"
        f"- Mention their website: {website}\n"
        f"- Mention WHY they are a good fit using this reason: {reason}\n"
        "- Offer AI automation / lead generation help\n"
        "- Friendly human tone\n"
        "- Max 120 words\n"
        '- Include this CTA exactly: "Would you be open to a quick call this week?"\n\n'
        'Return only valid JSON:\n{"subject": "", "body": ""}\n'
    )
    parsed = run_claude_json(
        "You write concise, human B2B cold emails and return only valid JSON.",
        prompt,
        max_tokens=450,
        temperature=0.4,
    )
    subject = str(parsed.get("subject", "")).strip()
    body = str(parsed.get("body", "")).strip()
    if not subject or not body:
        raise RuntimeError("Could not generate cold email with Claude.")
    return subject, body


def generate_email_variants(
    business_info: Dict[str, Any],
    website_summary: str,
    lead_score: Any,
) -> Dict[str, Any]:
    company = str(
        business_info.get("company_name")
        or business_info.get("Company")
        or business_info.get("name")
        or "the company"
    ).strip()
    website = str(business_info.get("website") or business_info.get("Website") or "").strip()
    industry = str(business_info.get("industry") or business_info.get("Industry") or "").strip()
    score_text = str(lead_score).strip() or "0"
    summary = str(website_summary or "").strip()

    prompt = (
        "Create 3 short personalized B2B cold email variants for a sales outreach campaign.\n\n"
        f"- Business name: {company}\n"
        f"- Website: {website}\n"
        f"- Industry: {industry}\n"
        f"- Website summary: {summary}\n"
        f"- Lead score: {score_text}\n"
        "- Emails should feel human and relevant\n"
        "- Keep each body under 120 words\n"
        "- Avoid spammy language\n"
        "- Use a different angle for each variant\n"
        "- Include one personalized hook based on the website summary\n"
        "- Include one clear CTA\n\n"
        "Return only valid JSON in this exact structure:\n"
        '{\n'
        '  "personalized_hook": "",\n'
        '  "cta": "",\n'
        '  "variants": [\n'
        '    {"subject": "", "body": "", "angle": ""},\n'
        '    {"subject": "", "body": "", "angle": ""},\n'
        '    {"subject": "", "body": "", "angle": ""}\n'
        "  ]\n"
        "}\n"
    )
    parsed = run_claude_json(
        "You write concise, personalized B2B outreach emails and return only valid JSON.",
        prompt,
        max_tokens=900,
        temperature=0.5,
    )
    variants = parsed.get("variants", [])
    if not isinstance(variants, list) or len(variants) < 3:
        raise RuntimeError("Could not generate 3 structured email variants.")
    normalized_variants = []
    for variant in variants[:3]:
        subject = str(variant.get("subject", "")).strip()
        body = str(variant.get("body", "")).strip()
        angle = str(variant.get("angle", "")).strip()
        if not subject or not body:
            raise RuntimeError("Generated email variant is missing subject or body.")
        normalized_variants.append({"subject": subject, "body": body, "angle": angle})
    return {
        "personalized_hook": str(parsed.get("personalized_hook", "")).strip(),
        "cta": str(parsed.get("cta", "")).strip(),
        "variants": normalized_variants,
    }


def build_gmail_message(to_email: str, subject: str, body: str, thread_id: str = "") -> Dict[str, Any]:
    message = MIMEText(body, "plain", "utf-8")
    message["To"] = to_email
    message["Subject"] = subject
    payload: Dict[str, Any] = {"raw": base64.urlsafe_b64encode(message.as_bytes()).decode()}
    if thread_id:
        payload["threadId"] = thread_id
    return payload


def send_email_gmail(to_email: str, subject: str, body: str, thread_id: str = "") -> Dict[str, Any]:
    service = get_gmail_service()
    payload = build_gmail_message(to_email, subject, body, thread_id=thread_id)
    return retry_operation(
        lambda: service.users().messages().send(userId="me", body=payload).execute(),
        f"Sending email to {to_email}",
    )


def append_unsubscribe_footer(body: str) -> str:
    normalized = str(body or "").rstrip()
    if UNSUBSCRIBE_FOOTER.strip().lower() in normalized.lower():
        return normalized
    return f"{normalized}{UNSUBSCRIBE_FOOTER}"


def is_opted_out_domain(email: str) -> bool:
    domain = str(email or "").strip().lower().split("@")[-1]
    blocked = {item.strip().lower() for item in os.getenv("EMAIL_OPTOUT_DOMAINS", "").split(",") if item.strip()}
    return bool(domain and domain in blocked)


async def authenticate_dashboard_user(tenant_id: str, email: str, password: str) -> Dict[str, Any]:
    async with get_async_db_session() as db:
        result = await AuthService(db).login(tenant_id=tenant_id, email=email, password=password)
        return {
            "tenant_id": result.tenant_id,
            "user_id": result.user_id,
            "email": result.email,
            "token": result.token,
            "subscription_plan": result.subscription_plan,
        }


def run_async_sync(coro):
    async def _runner():
        await reset_async_session_factory()
        try:
            return await coro
        finally:
            await reset_async_session_factory()

    return asyncio.run(_runner())


def authenticate_dashboard_user_sync(tenant_id: str, email: str, password: str) -> Dict[str, Any]:
    return run_async_sync(authenticate_dashboard_user(tenant_id=tenant_id, email=email, password=password))


async def fetch_dashboard_snapshot(tenant: TenantContext) -> Dict[str, Any]:
    async with get_async_db_session() as db:
        return await LeadService(db).dashboard_snapshot(tenant)


def fetch_dashboard_snapshot_sync(tenant: TenantContext) -> Dict[str, Any]:
    return run_async_sync(fetch_dashboard_snapshot(tenant))


def _lead_to_dashboard_row(lead: Lead) -> Dict[str, Any]:
    metadata = dict(lead.metadata or {})
    return {
        "Company": lead.company,
        "Website": lead.website,
        "Email": lead.email,
        "Location": lead.location,
        "Score": lead.score,
        "Reason": lead.reason,
        "Industry": lead.industry,
        "EmailStatus": lead.status.title(),
        "EmailSubject": str(metadata.get("EmailSubject", "")),
        "LastEmailBody": str(metadata.get("LastEmailBody", "")),
        "EmailSentAt": str(metadata.get("EmailSentAt", metadata.get("sent_at", ""))),
        "GmailThreadId": str(metadata.get("GmailThreadId", metadata.get("thread_id", ""))),
        "ReplyStatus": str(metadata.get("ReplyStatus", "No Reply")),
        "ReplyClassification": str(metadata.get("ReplyClassification", "No Reply")),
        "Sentiment": str(metadata.get("Sentiment", "Unknown")),
        "LeadTemperature": str(metadata.get("LeadTemperature", "")),
        "ReplyConfidenceScore": int(metadata.get("ReplyConfidenceScore", 0) or 0),
        "NextActionSuggestion": str(metadata.get("NextActionSuggestion", "")),
        "LastReply": str(metadata.get("LastReply", "")),
        "LastReplyAt": str(metadata.get("LastReplyAt", "")),
        "LastReplyFrom": str(metadata.get("LastReplyFrom", "")),
        "LastReplySnippet": str(metadata.get("LastReplySnippet", "")),
        "FollowupCount": int(metadata.get("FollowupCount", 0) or 0),
        "LastFollowupDate": str(metadata.get("LastFollowupDate", "")),
        "NextFollowupDue": str(metadata.get("NextFollowupDue", "")),
        "LastContactedAt": str(metadata.get("LastContactedAt", "")),
        "MeetingRequested": str(metadata.get("MeetingRequested", "No")),
    }


async def fetch_dashboard_rows(tenant: TenantContext) -> List[Dict[str, Any]]:
    async with get_async_db_session() as db:
        leads = await LeadService(db).list_leads(tenant)
        return [_lead_to_dashboard_row(lead) for lead in leads]


def fetch_dashboard_rows_sync(tenant: TenantContext) -> List[Dict[str, Any]]:
    return run_async_sync(fetch_dashboard_rows(tenant))


async def list_all_tenants() -> List[TenantContext]:
    async with get_async_db_session() as db:
        result = await db.session.execute(select(TenantRecord))
        return [TenantContext(tenant_id=item.tenant_id, tenant_slug=item.slug) for item in result.scalars().all()]


async def list_pending_outreach_leads(tenant: TenantContext) -> List[Lead]:
    async with get_async_db_session() as db:
        service = OutreachService(db)
        blocked_domains = {item.strip().lower() for item in os.getenv("EMAIL_OPTOUT_DOMAINS", "").split(",") if item.strip()}
        return await service.list_pending_outreach_leads(tenant, blocked_domains)


async def mark_outreach_result(lead: Lead, subject: str, body: str, message_id: str, thread_id: str, status: str) -> None:
    async with get_async_db_session() as db:
        tenant = TenantContext(tenant_id=lead.tenant_id)
        sent_at_dt = now_utc()
        await OutreachService(db).mark_outreach_result(
            tenant=tenant,
            lead=lead,
            subject=subject,
            body=body,
            message_id=message_id,
            thread_id=thread_id,
            status=status,
            sent_at_iso=to_iso8601(sent_at_dt),
            sent_at_dt=sent_at_dt,
        )


async def list_followup_candidates(tenant: TenantContext) -> List[Dict[str, Any]]:
    async with get_async_db_session() as db:
        blocked_domains = {item.strip().lower() for item in os.getenv("EMAIL_OPTOUT_DOMAINS", "").split(",") if item.strip()}
        return await OutreachService(db).list_followup_candidates(tenant, blocked_domains, now_utc(), parse_datetime)


async def save_followup_result(lead: Lead, last_email_id: str, subject: str, body: str, thread_id: str, followup_number: int) -> None:
    async with get_async_db_session() as db:
        now_dt = now_utc()
        next_due = ""
        if followup_number < 3:
            next_due = to_iso8601(now_dt + timedelta(days=2))
        await OutreachService(db).save_followup_result(
            tenant=TenantContext(tenant_id=lead.tenant_id),
            lead=lead,
            last_email_id=last_email_id,
            subject=subject,
            body=body,
            thread_id=thread_id,
            followup_number=followup_number,
            now_iso=to_iso8601(now_dt),
            now_dt=now_dt,
            next_due_iso=next_due,
        )


async def list_reply_candidates(tenant: TenantContext) -> List[Dict[str, Any]]:
    async with get_async_db_session() as db:
        return await OutreachService(db).list_reply_candidates(tenant)


async def save_reply_result(lead: Lead, email_id: str, message: Dict[str, Any], updates: Dict[str, str]) -> None:
    async with get_async_db_session() as db:
        await OutreachService(db).save_reply_result(
            tenant=TenantContext(tenant_id=lead.tenant_id),
            lead=lead,
            email_id=email_id,
            message=message,
            updates=updates,
            received_at=parse_datetime(str(updates.get("LastReplyAt", ""))),
        )


def decode_base64_message(data: str) -> str:
    try:
        padded = data + "=" * (-len(data) % 4)
        return base64.urlsafe_b64decode(padded.encode()).decode("utf-8", errors="ignore")
    except Exception:
        return ""


def extract_gmail_header(payload: Dict[str, Any], header_name: str) -> str:
    for header in payload.get("headers", []):
        if str(header.get("name", "")).lower() == header_name.lower():
            return str(header.get("value", "")).strip()
    return ""


def extract_message_text(payload: Dict[str, Any]) -> str:
    body_data = payload.get("body", {}).get("data", "")
    if body_data:
        return decode_base64_message(body_data)
    for part in payload.get("parts", []) or []:
        mime_type = part.get("mimeType", "")
        if mime_type == "text/plain":
            return decode_base64_message(part.get("body", {}).get("data", ""))
        if mime_type == "text/html":
            html = decode_base64_message(part.get("body", {}).get("data", ""))
            return clean_visible_text(html)
        nested_text = extract_message_text(part)
        if nested_text:
            return nested_text
    return ""


def clean_reply_text(text: str) -> str:
    cleaned_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(">"):
            continue
        if re.match(r"On .+wrote:", stripped):
            break
        cleaned_lines.append(line)
    cleaned = "\n".join(cleaned_lines).strip()
    return re.sub(r"\n{3,}", "\n\n", cleaned)


def extract_email_address(value: str) -> str:
    match = EMAIL_PATTERN.search(value or "")
    return match.group(0).lower() if match else ""


def get_gmail_message_metadata(message_id: str) -> Optional[Dict[str, Any]]:
    service = get_gmail_service()
    try:
        return service.users().messages().get(userId="me", id=message_id, format="full").execute()
    except HttpError as error:
        LOGGER.error("Could not fetch Gmail message %s: %s", message_id, error)
        return None


def get_thread_messages(thread_id: str) -> List[Dict[str, Any]]:
    service = get_gmail_service()
    try:
        thread = service.users().threads().get(userId="me", id=thread_id, format="full").execute()
        return thread.get("messages", [])
    except HttpError as error:
        LOGGER.error("Could not fetch Gmail thread %s: %s", thread_id, error)
        return []


def parse_gmail_internal_date(message: Dict[str, Any]) -> str:
    internal_date = str(message.get("internalDate", "")).strip()
    if not internal_date.isdigit():
        return ""
    dt_value = datetime.fromtimestamp(int(internal_date) / 1000, tz=UTC)
    return to_iso8601(dt_value)


def build_reply_analysis_payload(
    classification: str = "",
    sentiment: str = "",
    lead_temperature: str = "",
    reason: str = "",
    action_needed: bool = False,
    confidence_score: int = 0,
    next_action_suggestion: str = "",
) -> Dict[str, Any]:
    return {
        "classification": classification,
        "sentiment": sentiment,
        "lead_temperature": lead_temperature,
        "reason": reason,
        "action_needed": bool(action_needed),
        "confidence_score": max(0, min(100, int(confidence_score or 0))),
        "next_action_suggestion": next_action_suggestion,
    }


def analyze_reply_with_claude(company: str, sender_email: str, subject: str, reply_text: str) -> Dict[str, Any]:
    prompt = (
        "You are a sales inbox triage assistant.\n"
        "Classify the reply into exactly one of these values: Interested, Not Interested, Needs Follow-up, Spam.\n"
        "Use the message intent, sentiment, buying signals, and urgency.\n"
        "Interested means clear positive interest or meeting intent.\n"
        "Not Interested means clear rejection or no fit.\n"
        "Needs Follow-up means ambiguous interest, request for more info, delayed timing, or out-of-office style reply.\n"
        "Spam means irrelevant, automated junk, or obvious unwanted mail.\n"
        "Return STRICT JSON only in this exact schema:\n"
        '{\n  "classification": "",\n  "sentiment": "",\n  "lead_temperature": "hot/warm/cold",\n'
        '  "reason": "",\n  "action_needed": true,\n  "confidence_score": 0,\n  "next_action_suggestion": ""\n}\n\n'
        f"Company: {company}\nSender: {sender_email}\nSubject: {subject}\nReply:\n{reply_text}\n"
    )
    try:
        parsed = run_claude_json(
            "You classify sales email replies and return only strict JSON.",
            prompt,
            max_tokens=350,
            temperature=0,
        )
    except Exception as error:
        LOGGER.error("Reply classification failed: %s", error)
        return build_reply_analysis_payload(
            classification="Needs Follow-up",
            sentiment="neutral",
            lead_temperature="warm",
            reason="Claude analysis failed, so the reply was marked for manual review.",
            action_needed=True,
            confidence_score=20,
            next_action_suggestion="Review reply manually and send a clarification follow-up.",
        )

    return build_reply_analysis_payload(
        classification=str(parsed.get("classification", "")).strip(),
        sentiment=str(parsed.get("sentiment", "")).strip(),
        lead_temperature=str(parsed.get("lead_temperature", "")).strip(),
        reason=str(parsed.get("reason", "")).strip(),
        action_needed=bool(parsed.get("action_needed", False)),
        confidence_score=int(parsed.get("confidence_score", 0) or 0),
        next_action_suggestion=str(parsed.get("next_action_suggestion", "")).strip(),
    )


def generate_followup_email(lead: Dict[str, str], followup_number: int) -> Tuple[str, str]:
    company = lead.get("Company", "").strip()
    reason = lead.get("Reason", "").strip()
    previous_subject = lead.get("EmailSubject", "").strip()
    previous_body = lead.get("LastEmailBody", "").strip()
    followup_styles = {
        1: "Friendly reminder.",
        2: "More value-focused.",
        3: "Short closing loop email.",
    }
    style = followup_styles.get(followup_number, "Short closing loop email.")
    prompt = (
        "Write a personalized B2B sales follow-up email.\n"
        f"- Follow-up type: {style}\n"
        f"- Company: {company}\n"
        f"- Reason they were targeted: {reason}\n"
        f"- Previous subject: {previous_subject}\n"
        f"- Previous email: {previous_body}\n"
        "- Sound human\n"
        "- Avoid spammy language\n"
        "- Keep under 80 words\n"
        "- Reference the previous email naturally\n"
        "- Maintain a professional tone\n\n"
        'Return only valid JSON: {"subject": "", "body": ""}\n'
    )
    parsed = run_claude_json(
        "You write human follow-up emails and return only valid JSON.",
        prompt,
        max_tokens=220,
        temperature=0.5,
    )
    subject = str(parsed.get("subject", "")).strip() or f"Following up on my note to {company}"
    body = str(parsed.get("body", "")).strip()
    if not body:
        raise RuntimeError("Claude returned an empty follow-up email body.")
    return subject, append_unsubscribe_footer(body)


def run_email_outreach(tenant: TenantContext) -> None:
    async def _run() -> None:
        LOGGER.info("Starting AI Email Outreach...")
        pending_leads = await list_pending_outreach_leads(tenant)
        if not pending_leads:
            LOGGER.info("No pending email leads found.")
            return
        for lead in pending_leads:
            company = lead.company.strip() or "Unknown Company"
            email = lead.email.strip()
            if lead.status.lower() == "unsubscribed" or is_opted_out_domain(email):
                continue
            try:
                LOGGER.info("Running OutreachAgent for: %s", company)
                subject, body = generate_cold_email(
                    {
                        "Company": lead.company,
                        "Website": lead.website,
                        "Reason": lead.reason,
                    }
                )
                result = send_email_gmail(email, subject, append_unsubscribe_footer(body))
                await mark_outreach_result(
                    lead,
                    subject=subject,
                    body=append_unsubscribe_footer(body),
                    message_id=str(result.get("id", "")),
                    thread_id=str(result.get("threadId", "")),
                    status="sent",
                )
            except Exception as error:
                LOGGER.error("Outreach failed for %s (%s): %s", company, email, error)
                await mark_outreach_result(
                    lead,
                    subject="",
                    body="",
                    message_id="",
                    thread_id="",
                    status="failed",
                )
        LOGGER.info("Outreach complete.")

    run_async_sync(_run())


def dedupe_leads(leads: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    unique_leads = []
    seen = set()
    for lead in leads:
        website_key = get_website_key(str(lead.get("website", "")).strip() or str(lead.get("Website", "")).strip())
        company_key = str(lead.get("company_name", "") or lead.get("Company", "")).strip().lower()
        dedupe_key = website_key or company_key
        if not dedupe_key or dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        unique_leads.append(lead)
    return unique_leads


def beta_lead_readiness(analysis: Dict[str, Any]) -> str:
    email = str(analysis.get("email", analysis.get("verified_email", "")) or "").strip().lower()
    phone = str(analysis.get("phone", "") or "").strip()
    website = str(analysis.get("website", analysis.get("company_url", "")) or "").strip()
    company = str(analysis.get("company_name", analysis.get("company", "")) or "").strip()
    if is_valid_email(email):
        return "email_ready"
    if phone:
        return "phone_ready"
    if company or is_valid_company_website(website):
        return "research_needed"
    return ""


def is_qualified_lead(analysis: Dict[str, Any]) -> bool:
    website = str(analysis.get("website", analysis.get("company_url", "")) or "").strip()
    company = str(analysis.get("company_name", analysis.get("company", "")) or "").strip()
    if not company and not root_domain_from_url(website):
        return False
    if not is_valid_company_website(website):
        return False

    domain_type = str(analysis.get("domain_type", "") or "").strip().lower()
    if bool(analysis.get("is_directory", False)) or domain_type in {"excluded", "listing", "non_business"}:
        return False

    return bool(beta_lead_readiness(analysis))


def process_website(website: str, query: str = "", ai_mode: str = "") -> Dict[str, Any]:
    scraped = run_agent(ScraperAgent(), {"website": website})
    cleaned = run_agent(CleaningAgent(), scraped)
    cleaned["query"] = query
    cleaned["ai_mode"] = ai_mode
    scored = run_agent(ScoringAgent(), cleaned)
    result = scored["lead"]

    LOGGER.info("Scraping method for %s: %s", website, result.get("scraping_method", ""))
    if cleaned.get("fallback_reason"):
        LOGGER.info("Fallback reason for %s: %s", website, cleaned["fallback_reason"])
    LOGGER.info("Fit score breakdown for %s: %s", website, result["reason"])
    LOGGER.info("Final decision for %s: %s", website, result["decision"])
    return result


def process_query(
    query: str,
    seen_websites: Optional[set] = None,
    limit: Optional[int] = None,
    ai_mode: str = "",
    niche: str = "",
    location: str = "",
) -> List[Dict[str, Any]]:
    global LAST_PIPELINE_EVENTS, LAST_PIPELINE_STATS
    import logging as _lg; _lg.getLogger("TRACE").info("TRACE process_query called query=%s limit=%s niche=%s location=%s ai_mode=%s", query, limit, niche, location, ai_mode)
    leads = []
    skipped_count = 0
    events: List[Dict[str, Any]] = []
    stats: Dict[str, Any] = {
        "discovered_urls_count": 0,
        "scraped_pages_count": 0,
        "cleaned_records_count": 0,
        "extracted_emails_count": 0,
        "scored_leads_count": 0,
        "accepted_leads_count": 0,
        "rejected_leads_count": 0,
        "rejection_reasons": {},
    }
    if seen_websites is None:
        seen_websites = set()

    discovered = run_agent(
        DiscoveryAgent(),
        {
            "query": query,
            "niche": niche,
            "location": location,
            "seen_websites": sorted(seen_websites),
            "limit": limit,
        },
    )
    seen_websites.update(discovered.get("seen_websites", []))
    candidate_websites = list(discovered.get("websites", []))
    stats["discovered_urls_count"] = len(candidate_websites)
    _lg.getLogger("TRACE").info("TRACE DiscoveryAgent returned candidate_websites=%s raw_results=%s filtered=%s rejected_bad=%s rejected_irrelevant=%s", len(candidate_websites), discovered.get("raw_results_count"), discovered.get("filtered_results_count"), discovered.get("rejected_bad_domain_count"), discovered.get("rejected_irrelevant_count"))
    for key in (
        "final_query",
        "query_variants_count",
        "raw_results_count",
        "filtered_results_count",
        "rejected_bad_domain_count",
        "rejected_irrelevant_count",
        "duplicate_domain_count",
    ):
        stats[key] = discovered.get(key, 0 if key != "final_query" else "")
    events.extend(list(discovered.get("events", []) or []))

    if not candidate_websites:
        LOGGER.warning("process_query('%s') produced zero candidate websites after discovery.", query)
        LAST_PIPELINE_STATS = stats
        LAST_PIPELINE_EVENTS = events
        return leads

    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(1, len(candidate_websites)))) as executor:
        future_to_website = {executor.submit(process_website, website, query, ai_mode): website for website in candidate_websites}
        for future in as_completed(future_to_website):
            website = future_to_website[future]
            try:
                lead = future.result()
            except Exception as error:
                LOGGER.exception("Unexpected website processing failure for %s", website)
                reason = type(error).__name__
                stats["rejection_reasons"][reason] = int(stats["rejection_reasons"].get(reason, 0)) + 1
                events.append({"stage": "scraping", "status": "failed", "domain": root_domain_from_url(website), "url": website, "reason": "rejected_scrape_failed", "message": "scrape_failed"})
                continue
            events.append({"stage": "scraping", "status": "success", "domain": root_domain_from_url(website), "url": website, "message": "scrape_success"})
            stats["scraped_pages_count"] += 1
            stats["cleaned_records_count"] += 1
            stats["scored_leads_count"] += 1
            if str(lead.get("email", "")).strip():
                stats["extracted_emails_count"] += 1
            events.append(
                {
                    "stage": "extracting",
                    "status": "success",
                    "domain": root_domain_from_url(website),
                    "url": website,
                    "message": "emails_extracted",
                    "metadata": {
                        "emails_found_count": 1 if str(lead.get("email", "")).strip() else 0,
                        "phones_found_count": 1 if str(lead.get("phone", "")).strip() else 0,
                        "contact_page_found": bool(str(lead.get("contact_page", "")).strip()),
                    },
                }
            )
            if not bool(lead.get("qualified", False)) or str(lead.get("decision", "")).strip().lower() == "reject":
                skipped_count += 1
                stats["rejected_leads_count"] += 1
                reason = str(
                    lead.get("skip_reason")
                    or lead.get("quality_reason")
                    or lead.get("scrape_status")
                    or lead.get("email_status")
                    or "rejected"
                ).strip()
                stats["rejection_reasons"][reason] = int(stats["rejection_reasons"].get(reason, 0)) + 1
                LOGGER.info(
                    "Skipping rejected lead for %s (qualified=%s, decision=%s, reason=%s)",
                    website,
                    lead.get("qualified", False),
                    lead.get("decision", ""),
                    lead.get("skip_reason", lead.get("quality_reason", "")),
                )
                events.append({"stage": "scoring", "status": "rejected", "domain": root_domain_from_url(website), "url": website, "reason": reason, "message": "lead_rejected"})
                continue
            block_reason = blocklisted_lead_reason(lead, query)
            if block_reason:
                LOGGER.debug("Skipping blocklisted lead for %s: %s", website, block_reason)
                continue
            leads.append(lead)
            stats["accepted_leads_count"] += 1
            events.append({"stage": "scoring", "status": "accepted", "domain": root_domain_from_url(website), "url": website, "message": "lead_accepted"})
            LOGGER.info(
                "Lead stored: %s (score=%s, email=%s, total_saved=%s)",
                website,
                lead.get("lead_score", "n/a"),
                "yes" if lead.get("email") else "no",
                len(leads),
            )
            if limit is not None and len(leads) >= limit:
                break
    if not leads:
        LOGGER.warning(
            "process_query('%s') finished with zero leads. Discovery candidates=%s",
            query,
            len(candidate_websites),
        )
    LOGGER.info("process_query('%s') skipped %s rejected lead(s).", query, skipped_count)
    LAST_PIPELINE_STATS = stats
    LAST_PIPELINE_EVENTS = events
    return leads


def generate_leads(limit: int = DEFAULT_LEAD_LIMIT, ai_mode: str = "") -> List[Dict[str, Any]]:
    global GENERATED_LEADS
    load_environment()
    LOGGER.info("Generating leads...")

    queries = [
        "companies in Riyadh Saudi Arabia",
        "businesses in Jeddah Saudi Arabia",
        "companies in Dammam Saudi Arabia",
        "industrial companies in Saudi Arabia",
        "manufacturing companies in Saudi Arabia",
        "B2B companies in Saudi Arabia contact email",
        "Saudi Arabia logistics companies contact email",
        "Saudi Arabia construction companies contact email",
    ]

    all_leads: List[Dict[str, Any]] = []
    seen_websites = set()
    for query in queries:
        if len(all_leads) >= limit:
            break
        try:
            leads = process_query(query, seen_websites, limit=limit - len(all_leads), ai_mode=ai_mode)
            all_leads.extend(leads)
            LOGGER.info("Query complete: %s -> %s qualified lead(s) added.", query, len(leads))
        except Exception as error:
            LOGGER.error("Unexpected failure while processing '%s': %s", query, error)

    GENERATED_LEADS = dedupe_leads(all_leads)[:limit]
    LOGGER.info("Total qualified leads collected: %s", len(GENERATED_LEADS))
    return GENERATED_LEADS


def run_full_sales_assistant(limit: int = DEFAULT_LEAD_LIMIT, run_reply_agent: bool = True, run_followups: bool = True) -> None:
    from followup_agent import run_followups
    from reply_agent import check_email_replies

    generate_leads(limit=limit)
    LOGGER.info("Lead generation completed. Persist leads through tenant-aware SaaS agents for production execution.")


def main() -> None:
    load_environment()
    run_full_sales_assistant(limit=10, run_reply_agent=True, run_followups=True)


if __name__ == "__main__":
    main()
