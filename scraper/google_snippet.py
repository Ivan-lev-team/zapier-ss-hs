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


def _google_snippets(query: str) -> list[str]:
    """Fetch Google search and return all visible text snippets."""
    url = "https://www.google.com/search"
    params = {"q": query, "num": 5, "hl": "en", "gl": "us"}
    try:
        resp = requests.get(url, params=params, headers=_HEADERS, timeout=_TIMEOUT)
        if resp.status_code != 200:
            logger.debug("Google returned %d for query: %s", resp.status_code, query)
            return []
        html = resp.text

        # Pull text from <span> and <div> blocks — snippets live here
        # Strip all tags, collapse whitespace
        clean = re.sub(r"<[^>]+>", " ", html)
        clean = re.sub(r"\s+", " ", clean)

        # Split into chunks around revenue keywords for context
        chunks = re.split(r"(?i)\b(revenue|sales|annual|turnover|ARR)\b", clean)
        snippets = []
        for i, chunk in enumerate(chunks):
            # Grab the surrounding context (before + keyword + after)
            if re.match(r"(?i)revenue|sales|annual|turnover|ARR", chunk):
                ctx = " ".join(chunks[max(0, i-1):i+2])
                snippets.append(ctx[:300])

        return snippets
    except Exception as exc:
        logger.debug("Google snippet error for '%s': %s", query, exc)
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
            f"{clean_domain} site:crunchbase.com",
            f"{clean_domain} site:owler.com",
            f"{clean_domain} revenue",
            f"{clean_domain} annual revenue",
        ]
    queries += [
        f'"{company_name}" site:zoominfo.com',
        f'"{company_name}" annual revenue',
        f'"{company_name}" revenue',
    ]

    for query in queries:
        snippets = _google_snippets(query)
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
