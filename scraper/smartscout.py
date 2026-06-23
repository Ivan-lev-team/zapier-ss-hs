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
        p = _cache_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload))
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
    Extract T12M revenue from the first visible ag-grid row.

    SmartScout renders cells via Angular components — ag-cell.innerText is
    empty. Values live in span.clickable (primary) / span.secondary (sub-value).
    We identify the revenue column by its header text, then read that cell.

    ag-grid renders cell contents lazily, so retry a few times for the
    revenue cell to populate before giving up.
    """
    for attempt in range(5):
        raw = _read_revenue_cell(page)
        if raw:
            revenue = _parse_revenue(raw)
            if revenue is not None:
                logger.info("Revenue extracted: $%.2f", revenue)
                return revenue
        page.wait_for_timeout(1_500)  # let the cell lazy-render
    logger.info("Revenue cell stayed empty after retries")
    return None


def _read_revenue_cell(page: Page) -> str:
    raw: str = page.evaluate("""() => {
        // Step 1: find the col-id of the revenue column by matching header text
        let revenueColId = null;
        const headers = document.querySelectorAll('.ag-header-cell[col-id]');
        for (const h of headers) {
            const text = h.innerText.toLowerCase();
            if (text.includes('revenue') || text.includes('trailing')) {
                revenueColId = h.getAttribute('col-id');
                break;
            }
        }

        // Step 2: read span.clickable (primary value) from that column in row 0
        if (revenueColId) {
            const cell = document.querySelector(
                '.ag-row[row-index="0"] [col-id="' + revenueColId + '"]'
            );
            const span = cell && cell.querySelector('span.clickable:not(.secondary)');
            if (span && span.innerText.trim()) {
                return span.innerText.trim();
            }
        }

        // Fallback: first span.clickable in row 0 that looks like a dollar amount
        const row = document.querySelector('.ag-row[row-index="0"]');
        if (!row) return '';
        const spans = row.querySelectorAll('span.clickable:not(.secondary)');
        for (const s of spans) {
            if (s.innerText.includes('$')) return s.innerText.trim();
        }
        return '';
    }""")

    logger.info("Revenue cell raw text: %r", raw)
    return raw


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
# Query variant generation
# ---------------------------------------------------------------------------

def _build_query_variants(company_name: str, domain: str) -> list[str]:
    """
    Generate multiple search queries from a company name and domain.

    Examples for "Hydro Flask" / "hydroflask.com":
        → ["Hydro Flask", "HydroFlask", "hydroflask", "Hydro", "hydroflask"]

    Examples for "Dr. Squatch" / "drsquatch.com":
        → ["Dr. Squatch", "DrSquatch", "drsquatch", "Dr Squatch", "Squatch", "drsquatch"]
    """
    seen: set[str] = set()
    variants: list[str] = []
    _SKIP_WORDS = {"the", "a", "an", "and", "of", "in", "for", "by", "co", "inc", "llc", "ltd"}

    def _add(q: str) -> None:
        q = q.strip()
        if not q:
            return
        if q.lower() in seen:
            return
        # Skip standalone words that are too generic or too short to be meaningful
        if len(q) <= 3 or q.lower() in _SKIP_WORDS:
            return
        seen.add(q.lower())
        variants.append(q)

    # 1. Original name as-is
    _add(company_name)

    # 2. No spaces (HydroFlask)
    _add(company_name.replace(" ", ""))

    # 3. All lowercase no spaces (hydroflask)
    _add(company_name.replace(" ", "").lower())

    # 4. Strip punctuation: periods, commas, apostrophes (Dr. Squatch → Dr Squatch)
    cleaned = re.sub(r"[.,''`]", "", company_name)
    _add(cleaned)
    _add(cleaned.replace(" ", ""))

    # 5. With hyphens instead of spaces (Hydro-Flask)
    _add(company_name.replace(" ", "-"))

    # 6. Just the first word if multi-word (Hydro)
    words = company_name.split()
    if len(words) > 1:
        _add(words[0])
        # Also just the last word (Flask)
        _add(words[-1])

    # 7. Domain-based: strip TLD and www (hydroflask.com → hydroflask)
    root_domain = domain.lower().lstrip("www.").split("/")[0]
    domain_name = root_domain.rsplit(".", 1)[0] if "." in root_domain else root_domain
    _add(domain_name)

    return variants


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
            # SmartScout handles its own auth — never route through ZenRows proxy
            # (ZenRows causes SSL cert issues with SmartScout's Angular app)

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

            queries = _build_query_variants(company_name, domain)
            logger.info("Search variants for '%s': %s", company_name, queries)

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
