"""
StorLeads API — fallback revenue lookup for Shopify stores.

Public interface:
    get_shopify_revenue(company_name, domain) -> dict

Returns:
    {"revenue": float, "currency": "USD", "source": "storeleads", "domain_found": "...", "merchant_name": "..."}
    or
    {"revenue": None, "error": "not_found"}
    or
    {"revenue": None, "error": "api_error", "message": "..."}
"""

import logging
from typing import Optional

import requests

import config

logger = logging.getLogger(__name__)

_BASE_URL = "https://storeleads.app/json/api/v1"
_FIELDS = "name,state,platform,estimated_sales,estimated_sales_yearly,merchant_name"
_TIMEOUT = 15


def _headers() -> dict:
    return {"Authorization": f"Bearer {config.STORELEADS_API_KEY}"}


def _cents_to_dollars(cents: Optional[int]) -> Optional[float]:
    if cents is None:
        return None
    return round(cents / 100, 2)


def _extract_revenue(record: dict) -> Optional[float]:
    """Prefer yearly revenue, fall back to monthly * 12."""
    yearly = record.get("estimated_sales_yearly")
    if yearly:
        return _cents_to_dollars(yearly)
    monthly = record.get("estimated_sales")
    if monthly:
        return _cents_to_dollars(monthly * 12)
    return None


def _is_active_shopify(record: dict) -> bool:
    return (
        str(record.get("platform", "")).lower() == "shopify"
        and str(record.get("state", "")).lower() == "active"
    )


def _lookup_by_domain(domain: str) -> Optional[dict]:
    """Look up a store by exact domain. Returns the record dict or None."""
    if not domain:
        return None
    # Strip protocol/path — StorLeads expects bare domain
    clean = domain.lower().replace("https://", "").replace("http://", "").split("/")[0]
    url = f"{_BASE_URL}/all/domain/{clean}"
    try:
        resp = requests.get(url, headers=_headers(), params={"fields": _FIELDS}, timeout=_TIMEOUT)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        # Domain endpoint returns a single object (not a list)
        if isinstance(data, dict) and data.get("name"):
            return data
        return None
    except requests.HTTPError as exc:
        logger.warning("StorLeads domain lookup HTTP error (%s): %s", domain, exc)
        return None
    except Exception as exc:
        logger.warning("StorLeads domain lookup error (%s): %s", domain, exc)
        return None


def _lookup_by_company(company_name: str) -> Optional[dict]:
    """Search stores by company name. Returns the best active Shopify record or None."""
    if not company_name:
        return None
    url = f"{_BASE_URL}/all/company/{requests.utils.quote(company_name)}"
    try:
        resp = requests.get(url, headers=_headers(), params={"fields": _FIELDS}, timeout=_TIMEOUT)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        # Company endpoint returns a list of records in match-likelihood order
        records = data if isinstance(data, list) else []
        for record in records:
            if _is_active_shopify(record):
                return record
        return None
    except requests.HTTPError as exc:
        logger.warning("StorLeads company search HTTP error (%s): %s", company_name, exc)
        return None
    except Exception as exc:
        logger.warning("StorLeads company search error (%s): %s", company_name, exc)
        return None


def get_shopify_revenue(company_name: str, domain: str) -> dict:
    """
    Try domain lookup first, then company name search.
    Only accepts active Shopify stores.

    Returns revenue dict on success, or error dict on failure.
    """
    logger.info("StorLeads lookup — company='%s' domain='%s'", company_name, domain)

    # 1. Try domain lookup
    record = _lookup_by_domain(domain)
    if record:
        if _is_active_shopify(record):
            revenue = _extract_revenue(record)
            if revenue is not None:
                logger.info("StorLeads domain hit: %s → $%.2f", record.get("name"), revenue)
                return {
                    "revenue": revenue,
                    "currency": "USD",
                    "source": "storeleads",
                    "domain_found": record.get("name"),
                    "merchant_name": record.get("merchant_name"),
                }
        else:
            logger.info(
                "StorLeads domain found but platform=%s state=%s — skipping",
                record.get("platform"),
                record.get("state"),
            )

    # 2. Try company name search
    record = _lookup_by_company(company_name)
    if record:
        revenue = _extract_revenue(record)
        if revenue is not None:
            logger.info("StorLeads company hit: %s → $%.2f", record.get("name"), revenue)
            return {
                "revenue": revenue,
                "currency": "USD",
                "source": "storeleads",
                "domain_found": record.get("name"),
                "merchant_name": record.get("merchant_name"),
            }

    logger.info("StorLeads: no active Shopify record found for '%s' / '%s'", company_name, domain)
    return {"revenue": None, "error": "not_found"}
