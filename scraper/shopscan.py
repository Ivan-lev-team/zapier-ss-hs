"""
ShopScan revenue checker — scrapes https://www.shopscan.app/tool/shopify-store-revenue-checker
Uses ZenRows proxy to bypass CAPTCHA.

Public interface:
    get_shopify_revenue_shopscan(domain) -> dict
"""

import logging
import re
from typing import Optional

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

import config

logger = logging.getLogger(__name__)

_URL = "https://www.shopscan.app/tool/shopify-store-revenue-checker"
_TIMEOUT = 45_000  # ms

_REVENUE_RE = re.compile(
    r"\$\s*([\d,]+(?:\.\d+)?)\s*(thousand|million|billion|[KkMmBb])?",
    re.IGNORECASE,
)
_MULTIPLIERS = {
    "k": 1_000, "thousand": 1_000,
    "m": 1_000_000, "million": 1_000_000,
    "b": 1_000_000_000, "billion": 1_000_000_000,
}


def _parse_revenue(text: str) -> Optional[int]:
    match = _REVENUE_RE.search(text)
    if not match:
        return None
    number = float(match.group(1).replace(",", ""))
    suffix = (match.group(2) or "").lower()
    result = round(number * _MULTIPLIERS.get(suffix, 1))
    if result < 1_000 or result > 1_000_000_000_000:
        return None
    return result


def get_shopify_revenue_shopscan(domain: str) -> dict:
    """
    Load ShopScan revenue checker, submit domain, extract revenue.
    Requires ZenRows to bypass CAPTCHA (USE_ZENROWS=true in env).
    """
    if not config.USE_ZENROWS:
        logger.info("ShopScan: ZenRows disabled — skipping")
        return {"revenue": None, "error": "zenrows_disabled"}

    clean = domain.lower().replace("https://", "").replace("http://", "").split("/")[0]
    logger.info("ShopScan lookup — domain='%s'", clean)

    proxy = {
        "server": "http://api.zenrows.com:8001",
        "username": config.ZENROWS_API_KEY,
        "password": "js_render=true&premium_proxy=true",
    }

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationControlled"],
            proxy=proxy,
        )
        context = browser.new_context(
            ignore_https_errors=True,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        try:
            page.goto(_URL, wait_until="domcontentloaded", timeout=_TIMEOUT)
            logger.info("ShopScan: page loaded")

            # Domain input has id="domainInput"
            inp = page.locator("#domainInput")
            inp.wait_for(state="visible", timeout=20_000)
            inp.fill(clean)
            logger.info("ShopScan: entered domain '%s'", clean)

            # Submit the form — input is `required` so Enter submits it.
            # Try a submit button first, fall back to Enter.
            try:
                page.locator(
                    'button[type="submit"], button:has-text("Check"), '
                    'button:has-text("Analyze"), button:has-text("Get"), '
                    'button:has-text("Search"), button:has-text("Revenue")'
                ).first.click(timeout=4_000)
            except Exception:
                inp.press("Enter")

            # Wait for result to render — poll for a $ figure appearing
            logger.info("ShopScan: waiting for results...")
            page_text = ""
            for _ in range(12):  # up to ~24s
                page.wait_for_timeout(2_000)
                page_text = page.inner_text("body")
                if _parse_revenue(page_text):
                    break

            logger.info("ShopScan page text sample: %s", page_text[:600].replace("\n", " "))

            revenue = _parse_revenue(page_text)
            if revenue:
                logger.info("ShopScan hit: domain='%s' → $%d", clean, revenue)
                return {
                    "revenue": revenue,
                    "currency": "USD",
                    "source": "shopscan",
                    "domain_used": clean,
                }

            logger.info("ShopScan: no revenue found for '%s'", clean)
            return {"revenue": None, "error": "not_found"}

        except PWTimeout:
            logger.warning("ShopScan: timeout for domain '%s'", clean)
            return {"revenue": None, "error": "timeout"}
        except Exception as exc:
            logger.warning("ShopScan: error for '%s': %s", clean, exc)
            return {"revenue": None, "error": "scrape_error", "message": str(exc)}
        finally:
            browser.close()
