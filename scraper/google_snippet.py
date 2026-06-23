"""
Google snippet scraper — extracts revenue from Google search result snippets.

Searches for "{domain} site:zoominfo.com", "{domain} revenue", etc.
Revenue often appears in Google snippets from ZoomInfo/Crunchbase/Owler
without needing to log in to those sites.

Public interface:
    get_revenue_from_snippets(company_name, domain) -> dict
"""

import logging
import re
import time
from typing import Optional

import requests
from playwright.sync_api import sync_playwright

import config

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

_TIMEOUT = 10

# Matches: $1.2M, $500K, $1,234,567, $2.5 million, $10 billion, etc.
_REVENUE_RE = re.compile(
    r"\$\s*([\d,]+(?:\.\d+)?)\s*"
    r"(thousand|million|billion|trillion|[KkMmBbTt])?\b",
    re.IGNORECASE,
)
_MULTIPLIERS = {
    "k": 1_000, "thousand": 1_000,
    "m": 1_000_000, "million": 1_000_000,
    "b": 1_000_000_000, "billion": 1_000_000_000,
    "t": 1_000_000_000_000, "trillion": 1_000_000_000_000,
}


def _parse_revenue(text: str) -> Optional[int]:
    match = _REVENUE_RE.search(text)
    if not match:
        return None
    number = float(match.group(1).replace(",", ""))
    suffix = (match.group(2) or "").lower()
    multiplier = _MULTIPLIERS.get(suffix, 1)
    result = round(number * multiplier)
    # Sanity check: ignore implausible values (< $1K or > $1T)
    if result < 1_000 or result > 1_000_000_000_000:
        return None
    return result


def _google_snippets_zenrows(query: str) -> str:
    """Fetch Google results via ZenRows proxy (bypasses Google's bot detection)."""
    if not (config.USE_ZENROWS and config.ZENROWS_API_KEY):
        return ""
    url = "https://api.zenrows.com/v1/"
    params = {
        "apikey": config.ZENROWS_API_KEY,
        "url": f"https://www.google.com/search?q={requests.utils.quote(query)}&num=10&hl=en&gl=us",
        "premium_proxy": "true",
        "js_render": "true",
    }
    try:
        resp = requests.get(url, params=params, timeout=60)
        logger.info("ZenRows Google status=%d len=%d for '%s'", resp.status_code, len(resp.text), query)
        if resp.status_code != 200:
            logger.warning("ZenRows Google fetch returned %d body=%s", resp.status_code, resp.text[:300])
            return ""
        # Strip HTML tags to plain text
        text = re.sub(r"<[^>]+>", " ", resp.text)
        text = re.sub(r"\s+", " ", text)
        logger.info("ZenRows Google stripped text sample: %s", text[:500])
        return text
    except Exception as exc:
        logger.warning("ZenRows Google error for '%s': %s", query, exc)
        return ""


def _google_snippets_playwright(query: str) -> list[str]:
    """Fetch Google results — via ZenRows if enabled, else direct Playwright."""
    try:
        text = _google_snippets_zenrows(query)

        if not text:
            # Fallback: direct Playwright (likely blocked on datacenter IPs)
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage",
                          "--disable-blink-features=AutomationControlled"],
                )
                context = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    locale="en-US",
                )
                page = context.new_page()
                url = f"https://www.google.com/search?q={requests.utils.quote(query)}&num=5&hl=en&gl=us"
                page.goto(url, wait_until="domcontentloaded", timeout=15_000)
                page.wait_for_timeout(2_000)
                text = page.inner_text("body")
                browser.close()

        logger.info("Google snippet text sample for '%s': %s", query, text[:300].replace("\n", " "))

        # Split around revenue keywords and grab context
        chunks = re.split(r"(?i)\b(revenue|sales|annual|turnover|ARR)\b", text)
        snippets = []
        for i, chunk in enumerate(chunks):
            if re.match(r"(?i)revenue|sales|annual|turnover|ARR", chunk):
                ctx = " ".join(chunks[max(0, i-1):i+2])
                snippets.append(ctx[:400])
        return snippets
    except Exception as exc:
        logger.warning("Google snippet Playwright error for '%s': %s", query, exc)
        return []


def get_revenue_from_snippets(company_name: str, domain: str) -> dict:
    """
    Try several Google queries and parse revenue from snippets.
    Fast, no login required — works because revenue appears in Google previews.
    """
    logger.info("Google snippet lookup — company='%s' domain='%s'", company_name, domain)

    clean_domain = ""
    if domain:
        clean_domain = domain.lower().replace("https://", "").replace("http://", "").split("/")[0]

    queries = []
    if clean_domain:
        queries += [
            f"{clean_domain} site:zoominfo.com",
            f"{clean_domain} revenue",
        ]
    queries.append(f'"{company_name}" annual revenue')

    for query in queries:
        snippets = _google_snippets_playwright(query)
        for snippet in snippets:
            revenue = _parse_revenue(snippet)
            if revenue:
                logger.info(
                    "Google snippet hit: query='%s' → $%d | snippet: %s",
                    query, revenue, snippet[:120],
                )
                return {
                    "revenue": revenue,
                    "currency": "USD",
                    "source": "google_snippet",
                    "query_used": query,
                }
        # Small delay between queries to avoid rate limiting
        time.sleep(1)

    logger.info("Google snippet: no revenue found for '%s' / '%s'", company_name, domain)
    return {"revenue": None, "error": "not_found"}
