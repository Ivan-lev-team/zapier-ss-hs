"""
ShopScan revenue checker — intercepts the JSON API response from
https://www.shopscan.app/tool/shopify-store-revenue-checker

The page uses Cloudflare Turnstile so we need Playwright + ZenRows proxy.
We intercept the /api/shopify-revenue-checker-handler.php response to get
estimated_sales_yearly directly from JSON instead of parsing page text.

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
_TIMEOUT = 60_000  # ms

_REVENUE_RE = re.compile(
    r"USD\s*\$\s*([\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)


def _parse_usd(text: str) -> Optional[int]:
    m = _REVENUE_RE.search(text)
    if not m:
        return None
    val = round(float(m.group(1).replace(",", "")))
    if val < 1_000 or val > 1_000_000_000_000:
        return None
    return val


def get_shopify_revenue_shopscan(domain: str) -> dict:
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

    captured = {}

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

        def _on_response(response):
            if "shopify-revenue-checker-handler" in response.url:
                try:
                    data = response.json()
                    captured["data"] = data
                    logger.info("ShopScan API response intercepted: status=%s", response.status)
                except Exception as exc:
                    logger.warning("ShopScan: failed to parse intercepted response: %s", exc)

        page.on("response", _on_response)

        try:
            page.goto(_URL, wait_until="domcontentloaded", timeout=_TIMEOUT)
            logger.info("ShopScan: page loaded — url=%s", page.url)

            inp = page.locator("#domainInput")
            inp.wait_for(state="attached", timeout=30_000)
            inp.fill(clean, force=True)
            logger.info("ShopScan: entered domain '%s'", clean)

            try:
                page.locator(
                    'button[type="submit"], button:has-text("Check"), '
                    'button:has-text("Analyze"), button:has-text("Get"), '
                    'button:has-text("Search"), button:has-text("Revenue")'
                ).first.click(timeout=5_000)
            except Exception:
                inp.press("Enter")

            # Wait for API response to be intercepted (up to 30s)
            for _ in range(15):
                page.wait_for_timeout(2_000)
                if captured:
                    break

            if not captured:
                logger.info("ShopScan: no API response intercepted for '%s'", clean)
                return {"revenue": None, "error": "not_found"}

            data = captured["data"]
            domain_data = data.get("data", {}).get("domain", {})

            yearly = domain_data.get("estimated_sales_yearly", "")
            monthly = domain_data.get("estimated_sales", "")

            revenue = _parse_usd(yearly) or _parse_usd(monthly)

            if revenue:
                logger.info("ShopScan hit: domain='%s' → $%d (yearly=%s)", clean, revenue, yearly)
                return {
                    "revenue": revenue,
                    "currency": "USD",
                    "source": "shopscan",
                    "domain_used": clean,
                }

            logger.info("ShopScan: no revenue in response for '%s'. yearly=%r", clean, yearly)
            return {"revenue": None, "error": "not_found"}

        except PWTimeout:
            logger.warning("ShopScan: timeout for domain '%s'", clean)
            return {"revenue": None, "error": "timeout"}
        except Exception as exc:
            logger.warning("ShopScan: error for '%s': %s", clean, exc)
            return {"revenue": None, "error": "scrape_error", "message": str(exc)}
        finally:
            browser.close()
