"""
LeadMagic API — company revenue lookup (third fallback after SmartScout + StorLeads).

Public interface:
    get_company_revenue(company_name, domain) -> dict

Returns:
    {"revenue": int, "currency": "USD", "source": "leadmagic", "company_found": "..."}
    or
    {"revenue": None, "error": "not_found"}
    or
    {"revenue": None, "error": "api_error", "message": "..."}
"""

import logging
import re
from typing import Optional

import requests

import config

logger = logging.getLogger(__name__)

_API_URL = "https://api.leadmagic.io/company-search"
_TIMEOUT = 15

# LeadMagic revenue range strings → midpoint in dollars
# e.g. "$1M-$10M" → 5_500_000
_RANGE_RE = re.compile(r"\$?([\d.]+)\s*([KkMmBb]?)\s*[-–]\s*\$?([\d.]+)\s*([KkMmBb]?)")
_SINGLE_RE = re.compile(r"\$?([\d,.]+)\s*([KkMmBb]?)")
_MULTIPLIERS = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}


def _to_dollars(number: float, suffix: str) -> float:
    return number * _MULTIPLIERS.get(suffix.lower(), 1)


def _parse_revenue(value) -> Optional[int]:
    """Parse revenue from int, float, or range string. Returns rounded int dollars."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return round(value)

    text = str(value).strip()
    if not text or text in ("-", "N/A", "Unknown"):
        return None

    # Range: "$1M-$10M" → midpoint
    m = _RANGE_RE.search(text)
    if m:
        lo = _to_dollars(float(m.group(1)), m.group(2))
        hi = _to_dollars(float(m.group(3)), m.group(4))
        return round((lo + hi) / 2)

    # Single value
    m = _SINGLE_RE.search(text)
    if m:
        num = float(m.group(1).replace(",", ""))
        return round(_to_dollars(num, m.group(2)))

    return None


def _extract_revenue(data: dict) -> Optional[int]:
    """Try common field names LeadMagic uses for revenue."""
    for field in ("annual_revenue", "revenue", "estimated_revenue", "yearly_revenue",
                  "company_revenue", "revenue_range"):
        val = data.get(field)
        parsed = _parse_revenue(val)
        if parsed is not None and parsed > 0:
            return parsed
    return None


def get_company_revenue(company_name: str, domain: str) -> dict:
    """
    Look up company revenue via LeadMagic company search API.
    Tries with domain first, falls back to name-only if needed.
    """
    logger.info("LeadMagic lookup — company='%s' domain='%s'", company_name, domain)

    headers = {
        "Content-Type": "application/json",
        "x-api-key": config.LEADMAGIC_API_KEY,
    }

    # Build payload — include domain if available
    payload = {"company_name": company_name}
    if domain:
        clean = domain.lower().replace("https://", "").replace("http://", "").split("/")[0]
        payload["company_website"] = clean

    try:
        resp = requests.post(_API_URL, json=payload, headers=headers, timeout=_TIMEOUT)

        if resp.status_code == 404:
            logger.info("LeadMagic: not found for '%s'", company_name)
            return {"revenue": None, "error": "not_found"}

        resp.raise_for_status()
        data = resp.json()

        # Log full response for debugging (first time)
        logger.debug("LeadMagic raw response: %s", data)

        # Handle list vs single object response
        record = data[0] if isinstance(data, list) and data else data if isinstance(data, dict) else {}

        company_found = record.get("name") or record.get("company_name") or company_name
        revenue = _extract_revenue(record)

        if revenue is not None:
            logger.info("LeadMagic hit: %s → $%d", company_found, revenue)
            return {
                "revenue": revenue,
                "currency": "USD",
                "source": "leadmagic",
                "company_found": company_found,
            }

        logger.info("LeadMagic: found company but no revenue data for '%s'. Fields: %s",
                    company_name, list(record.keys()))
        return {"revenue": None, "error": "not_found"}

    except requests.HTTPError as exc:
        logger.warning("LeadMagic HTTP error (%s): %s", company_name, exc)
        return {"revenue": None, "error": "api_error", "message": str(exc)}
    except Exception as exc:
        logger.warning("LeadMagic error (%s): %s", company_name, exc)
        return {"revenue": None, "error": "api_error", "message": str(exc)}
