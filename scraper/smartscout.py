"""
SmartScout scraper — extracts T12M Amazon revenue for a given brand.

Public interface:
    get_t12m_revenue(company_name, domain) -> dict
"""

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from playwright.sync_api import sync_playwright, Page, BrowserContext, TimeoutError as PWTimeout

import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Revenue string parsing
# ---------------------------------------------------------------------------

_REVENUE_RE = re.compile(
    r"\$\s*([\d,]+(?:\.\d+)?)\s*([KkMmBb]?)",
    re.IGNORECASE,
)

_MULTIPLIERS = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}


def _parse_revenue(text: str) -> Optional[float]:
    """Return float dollars from strings like '$1.2M', '$500K', '$1,234,567'."""
    match = _REVENUE_RE.search(text)
    if not match:
        return None
    number = float(match.group(1).replace(",", ""))
    suffix = match.group(2).lower()
    multiplier = _MULTIPLIERS.get(suffix, 1)
    return number * multiplier


# ---------------------------------------------------------------------------
# Session cookie cache
# ---------------------------------------------------------------------------

def _cache_path() -> Path:
    return Path(config.SESSION_CACHE_PATH)


def _load_cached_cookies() -> Optional[list]:
    path = _cache_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        saved_at = datetime.fromisoformat(data["saved_at"])
        age_hours = (datetime.now(timezone.utc) - saved_at).total_seconds() / 3600
        if age_hours > config.SESSION_MAX_AGE_HOURS:
            logger.info("Session cache expired (%.1fh old)", age_hours)
            path.unlink(missing_ok=True)
            return None
        return data["cookies"]
    except Exception as exc:
        logger.warning("Could not read session cache: %s", exc)
        return None


def _save_cookies(context: BrowserContext) -> None:
    cookies = context.cookies()
    payload = {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "cookies": cookies,
    }
    try:
        _cache_path().write_text(json.dumps(payload))
        logger.debug("Session cookies saved (%d cookies)", len(cookies))
    except Exception as exc:
        logger.warning("Could not save session cache: %s", exc)


def _clear_cookie_cache() -> None:
    _cache_path().unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Browser factory
# ---------------------------------------------------------------------------

def _browser_launch_args() -> list:
    return [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled",
    ]


def _zenrows_proxy() -> dict:
    return {
        "server": "http://api.zenrows.com:8001",
        "username": config.ZENROWS_API_KEY,
        "password": "js_render=true&premium_proxy=true",
    }


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

def _login(page: Page) -> None:
    logger.info("Logging in to SmartScout as %s", config.SS_EMAIL)
    page.goto(f"{config.SMARTSCOUT_BASE_URL}/sessions/signin", wait_until="domcontentloaded",
              timeout=config.REQUEST_TIMEOUT_MS)

    page.locator('#username').fill(config.SS_EMAIL, timeout=10_000)
    page.locator('input[type="password"]').fill(config.SS_PASSWORD, timeout=10_000)

    # Angular component intercepts pointer events — JS click bypasses it
    page.evaluate("document.getElementById('btnSignin').click()")

    try:
        page.wait_for_url(
            lambda url: "/sessions/signin" not in url,
            timeout=config.REQUEST_TIMEOUT_MS,
        )
    except PWTimeout:
        raise RuntimeError("Login did not redirect — check credentials or SmartScout URL")

    logger.info("Login successful — URL: %s", page.url)


def _is_authenticated(page: Page) -> bool:
    try:
        page.goto(f"{config.SMARTSCOUT_BASE_URL}/app/brands", wait_until="domcontentloaded",
                  timeout=config.REQUEST_TIMEOUT_MS)
        # Wait for Angular router to complete any auth-guard redirect before checking
        page.wait_for_timeout(3000)
        authenticated = "/sessions/signin" not in page.url
        logger.info("Auth check — URL: %s — authenticated: %s", page.url, authenticated)
        return authenticated
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Search & extract
# ---------------------------------------------------------------------------

_T12M_LABEL_PATTERNS = [
    re.compile(r"trailing\s+12", re.IGNORECASE),
    re.compile(r"12[\s\-]?month", re.IGNORECASE),
    re.compile(r"t12m", re.IGNORECASE),
]


def _find_revenue_on_page(page: Page) -> Optional[float]:
    """
    Extract T12M revenue from the current page.

    Strategy 1 — read directly from the first ag-grid row (targeted, preferred).
    Strategy 2 — full page text scan near T12M column headers (fallback).
    """
    # Strategy 1: first ag-grid data row contains the revenue figure we just filtered to
    try:
        first_row_text = page.locator('.ag-row[row-index="0"]').inner_text(timeout=3_000)
        revenue = _parse_revenue(first_row_text)
        if revenue is not None:
            logger.debug("Revenue from first ag-row: $%.2f", revenue)
            return revenue
    except Exception:
        pass

    # Strategy 2: scan body text for dollar amounts near T12M column header labels
    full_text = page.inner_text("body")
    lines = full_text.splitlines()
    for i, line in enumerate(lines):
        if any(p.search(line) for p in _T12M_LABEL_PATTERNS):
            context_text = " ".join(lines[i : i + 3])
            revenue = _parse_revenue(context_text)
            if revenue is not None:
                logger.debug("Revenue from text scan: $%.2f", revenue)
                return revenue

    return None


def _search_brand(page: Page, query: str) -> bool:
    """
    Filter the SmartScout brands grid by query.
    Returns True if at least one row is visible after filtering.
    """
    logger.info("Searching SmartScout for: %s", query)

    page.goto(f"{config.SMARTSCOUT_BASE_URL}/app/brands", wait_until="domcontentloaded",
              timeout=config.REQUEST_TIMEOUT_MS)
    page.wait_for_timeout(3000)  # wait for ag-grid to render

    if "/sessions/signin" in page.url:
        logger.warning("Redirected to login during brand search — session expired mid-run")
        return False

    # "Brand Names" is the ag-grid column filter — confirmed via DOM inspection
    try:
        search_input = page.locator('input[placeholder="Brand Names"]').first
        search_input.wait_for(timeout=8_000)
    except PWTimeout:
        logger.warning("Brand Names filter input not found on brands page")
        return False

    search_input.click()
    search_input.fill(query)
    page.wait_for_timeout(3000)  # ag-grid filters in-place as you type

    # Confirm rows are visible
    try:
        page.wait_for_selector('.ag-row', timeout=8_000)
    except PWTimeout:
        logger.warning("No ag-grid rows visible after filtering for: %s", query)
        return False

    visible_rows = page.locator('.ag-row:not(.ag-hidden)').count()
    logger.info("Rows visible after filtering for '%s': %d", query, visible_rows)

    return visible_rows > 0


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def get_t12m_revenue(company_name: str, domain: str) -> dict:
    """
    Log into SmartScout and return the T12M Amazon revenue for the given brand.

    Returns:
        On success: {"revenue": 1234567.89, "currency": "USD", "source": "smartscout",
                     "company_name": "...", "query_used": "..."}
        On failure: {"revenue": None, "error": "<reason>", "company_name": "..."}
    """
    start = time.monotonic()
    result_base = {"company_name": company_name}

    try:
        with sync_playwright() as pw:
            launch_kwargs: dict = {
                "headless": True,
                "args": _browser_launch_args(),
            }
            if config.USE_ZENROWS:
                launch_kwargs["proxy"] = _zenrows_proxy()

            browser = pw.chromium.launch(**launch_kwargs)
            context = browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
            )
            page = context.new_page()

            # Session management
            cached = _load_cached_cookies()
            if cached:
                context.add_cookies(cached)
                if not _is_authenticated(page):
                    logger.info("Cached session invalid — re-logging in")
                    _clear_cookie_cache()
                    _login(page)
                    _save_cookies(context)
                else:
                    logger.info("Reused cached session")
            else:
                _login(page)
                _save_cookies(context)

            # Try company name first, then root domain as fallback
            queries = [company_name]
            root_domain = domain.lstrip("www.").split("/")[0]
            if root_domain and root_domain.lower() not in company_name.lower():
                queries.append(root_domain)

            revenue = None
            query_used = None
            for query in queries:
                found = _search_brand(page, query)
                if found:
                    revenue = _find_revenue_on_page(page)
                    if revenue is not None:
                        query_used = query
                        break
                    logger.info("Rows found for '%s' but no revenue extracted", query)
                else:
                    logger.info("No rows found for query: %s", query)

            context.close()
            browser.close()

        elapsed = time.monotonic() - start
        if revenue is not None:
            logger.info("Scraped T12M for '%s': $%.2f (%.1fs, query='%s')",
                        company_name, revenue, elapsed, query_used)
            return {
                **result_base,
                "revenue": revenue,
                "currency": "USD",
                "source": "smartscout",
                "query_used": query_used,
            }

        logger.warning("Revenue not found for '%s' after %.1fs", company_name, elapsed)
        return {**result_base, "revenue": None, "error": "not_found"}

    except PWTimeout as exc:
        elapsed = time.monotonic() - start
        logger.error("Timeout scraping '%s' after %.1fs: %s", company_name, elapsed, exc)
        return {**result_base, "revenue": None, "error": "timeout"}
    except Exception as exc:
        elapsed = time.monotonic() - start
        logger.error("Error scraping '%s' after %.1fs: %s", company_name, elapsed, exc, exc_info=True)
        return {**result_base, "revenue": None, "error": str(exc)}
