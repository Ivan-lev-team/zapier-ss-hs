"""
SmartScout scraper — extracts T12M Amazon revenue for a given brand.

Public interface:
    get_t12m_revenue(company_name, domain) -> dict
"""

import json
import logging
import os
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
# Browser / context factory
# ---------------------------------------------------------------------------

def _browser_launch_args() -> list:
    return [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled",
    ]


def _zenrows_proxy() -> dict:
    """ZenRows proxy config for Playwright."""
    return {
        "server": "http://api.zenrows.com:8001",
        "username": config.ZENROWS_API_KEY,
        "password": "js_render=true&premium_proxy=true",
    }


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

def _screenshot(page: Page, name: str) -> None:
    """Save a debug screenshot to the current directory."""
    path = f"debug_{name}.png"
    try:
        page.screenshot(path=path, full_page=True, timeout=10_000)
        logger.info("Screenshot saved: %s", path)
    except Exception as exc:
        logger.warning("Could not save screenshot: %s", exc)


def _login(page: Page) -> None:
    logger.info("Logging in to SmartScout as %s", config.SS_EMAIL)
    # Real login URL — /login redirects to /sessions/404
    page.goto(f"{config.SMARTSCOUT_BASE_URL}/sessions/signin", wait_until="networkidle",
              timeout=config.REQUEST_TIMEOUT_MS)

    logger.info("Login page loaded — URL: %s", page.url)

    # SmartScout uses id="username" for the email field (Angular app)
    page.locator('#username').fill(config.SS_EMAIL, timeout=10_000)
    logger.info("Filled username field")

    page.locator('input[type="password"]').fill(config.SS_PASSWORD, timeout=10_000)
    logger.info("Filled password field")

    # Click the Sign In button
    page.locator('button[type="submit"], button:has-text("Sign In"), button:has-text("Login")').first.click()
    logger.info("Clicked sign in button")

    # Wait for redirect away from the signin page
    try:
        page.wait_for_url(
            lambda url: "/sessions/signin" not in url,
            timeout=config.REQUEST_TIMEOUT_MS,
        )
    except PWTimeout:
        _screenshot(page, "login_failed")
        raise RuntimeError("Login did not redirect — check credentials or see debug_login_failed.png")

    logger.info("Login successful — URL: %s", page.url)


def _is_authenticated(page: Page) -> bool:
    """Navigate to a protected page; return True if we stay logged in."""
    try:
        page.goto(f"{config.SMARTSCOUT_BASE_URL}/brands", wait_until="domcontentloaded",
                  timeout=config.REQUEST_TIMEOUT_MS)
        return "/sessions/signin" not in page.url
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Search & extract
# ---------------------------------------------------------------------------

_T12M_LABEL_PATTERNS = [
    re.compile(r"12[\s\-]?month", re.IGNORECASE),
    re.compile(r"t12m", re.IGNORECASE),
    re.compile(r"trailing\s+12", re.IGNORECASE),
    re.compile(r"annual.*rev", re.IGNORECASE),
]


def _find_revenue_on_page(page: Page) -> Optional[float]:
    """
    Scan page text for a revenue figure near a T12M label.
    SmartScout shows revenue prominently on brand detail pages — try several
    strategies in order of confidence.
    """
    # Strategy 1: look for a stat card/tile whose label contains "12 month"
    for label_pattern in _T12M_LABEL_PATTERNS:
        elements = page.locator("*").filter(has_text=label_pattern).all()
        for el in elements[:10]:  # cap to avoid huge DOM walks
            # Check the element's text and nearby siblings/parent for a dollar amount
            parent_text = el.evaluate("el => el.closest('[class]')?.innerText || ''")
            revenue = _parse_revenue(parent_text)
            if revenue is not None:
                logger.debug("Found revenue via label match: $%.0f", revenue)
                return revenue

    # Strategy 2: scan all text nodes for dollar amounts near "revenue" keywords
    full_text = page.inner_text("body")
    lines = full_text.splitlines()
    for i, line in enumerate(lines):
        if any(p.search(line) for p in _T12M_LABEL_PATTERNS):
            # Check this line and the next two for a dollar figure
            context_text = " ".join(lines[i : i + 3])
            revenue = _parse_revenue(context_text)
            if revenue is not None:
                logger.debug("Found revenue via text scan: $%.0f", revenue)
                return revenue

    return None


def _search_brand(page: Page, query: str) -> bool:
    """
    Type query into the SmartScout brand search and click the first result.
    Returns True if a result was found and clicked.
    """
    logger.info("Searching SmartScout for: %s", query)
    page.goto(f"{config.SMARTSCOUT_BASE_URL}/brands", wait_until="domcontentloaded",
              timeout=config.REQUEST_TIMEOUT_MS)
    page.wait_for_timeout(3000)  # let Angular finish rendering

    # SmartScout's search input — selectors ordered by specificity
    search_selectors = [
        'input[placeholder*="search" i]',
        'input[placeholder*="brand" i]',
        'input[type="search"]',
        'input[type="text"]',
    ]
    search_input = None
    for sel in search_selectors:
        try:
            el = page.locator(sel).first
            el.wait_for(timeout=5_000)
            search_input = el
            break
        except PWTimeout:
            continue

    if search_input is None:
        logger.warning("Could not find search input on /brands page")
        return False

    search_input.click()
    search_input.fill(query)
    search_input.press("Enter")

    # Wait for results to appear
    try:
        page.wait_for_selector(
            'table tbody tr, [class*="result"], [class*="card"]',
            timeout=10_000,
        )
    except PWTimeout:
        logger.warning("No search results appeared for query: %s", query)
        return False

    # Click first result row/card
    first_result = page.locator(
        'table tbody tr:first-child, [class*="result"]:first-child, [class*="card"]:first-child'
    ).first
    try:
        first_result.click(timeout=5_000)
        page.wait_for_load_state("networkidle", timeout=config.REQUEST_TIMEOUT_MS)
        return True
    except PWTimeout:
        logger.warning("Could not click first result for query: %s", query)
        return False


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def get_t12m_revenue(company_name: str, domain: str) -> dict:
    """
    Log into SmartScout and return the T12M Amazon revenue for the given brand.

    Returns:
        On success: {"revenue": 1234567.89, "currency": "USD",
                     "source": "smartscout", "company_name": "...", "query_used": "..."}
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

            # --- Session management ---
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

            # --- Search: try company name, then root domain ---
            queries = [company_name]
            root_domain = domain.lstrip("www.").split("/")[0]
            if root_domain and root_domain not in company_name.lower():
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
                    logger.info("Brand page found for '%s' but no revenue data", query)
                else:
                    logger.info("No brand found for query: %s", query)

            context.close()
            browser.close()

        elapsed = time.monotonic() - start
        if revenue is not None:
            logger.info(
                "Scraped T12M revenue for '%s': $%.0f (%.1fs, query='%s')",
                company_name, revenue, elapsed, query_used,
            )
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
