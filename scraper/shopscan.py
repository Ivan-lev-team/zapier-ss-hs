"""
ShopScan revenue checker — uses ZenRows REST API with js_instructions to
fill the form and extract estimated_sales_yearly from the rendered page.

Public interface:
    get_shopify_revenue_shopscan(domain) -> dict
"""

import json
import logging
import re
from typing import Optional

import requests

import config

logger = logging.getLogger(__name__)

_URL = "https://www.shopscan.app/tool/shopify-store-revenue-checker"
_ZENROWS_API = "https://api.zenrows.com/v1/"
_TIMEOUT = 60

_REVENUE_RE = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)\s*(thousand|million|billion|[KkMmBb])?", re.IGNORECASE)


_MULTIPLIERS = {"k": 1_000, "thousand": 1_000, "m": 1_000_000, "million": 1_000_000, "b": 1_000_000_000, "billion": 1_000_000_000}

def _parse_usd(text: str) -> Optional[int]:
    m = _REVENUE_RE.search(text)
    if not m:
        return None
    num = float(m.group(1).replace(",", ""))
    suffix = (m.group(2) or "").lower()
    val = round(num * _MULTIPLIERS.get(suffix, 1))
    if val < 1_000 or val > 1_000_000_000_000:
        return None
    return val


def get_shopify_revenue_shopscan(domain: str) -> dict:
    if not config.USE_ZENROWS:
        logger.info("ShopScan: ZenRows disabled — skipping")
        return {"revenue": None, "error": "zenrows_disabled"}

    clean = domain.lower().replace("https://", "").replace("http://", "").split("/")[0]
    logger.info("ShopScan lookup — domain='%s'", clean)

    instructions = json.dumps([
        {"wait": 3000},
        {"fill": ["#domainInput", clean]},
        {"wait": 1000},
        {"click": "button"},
        {"wait": 20000},
    ])

    params = {
        "apikey": config.ZENROWS_API_KEY,
        "url": _URL,
        "js_render": "true",
        "premium_proxy": "true",
        "js_instructions": instructions,
    }

    try:
        logger.info("ShopScan: calling ZenRows with instructions=%s", instructions)
        resp = requests.get(_ZENROWS_API, params=params, timeout=90)
        logger.info("ShopScan ZenRows status=%d len=%d", resp.status_code, len(resp.text))

        if resp.status_code != 200:
            logger.warning("ShopScan ZenRows error: %s", resp.text[:300])
            return {"revenue": None, "error": "api_error", "message": resp.text[:200]}

        # Strip HTML tags and search for revenue
        text = re.sub(r"<[^>]+>", " ", resp.text)
        text = re.sub(r"\s+", " ", text)
        logger.info("ShopScan page text sample: %s", text[:2000])

        revenue = _parse_usd(text)
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

    except Exception as exc:
        logger.warning("ShopScan error for '%s': %s", clean, exc)
        return {"revenue": None, "error": "scrape_error", "message": str(exc)}
