"""
Claude web search — last-resort revenue lookup using Claude with web search tool.

Public interface:
    get_revenue_via_web(company_name, domain, amazon_storefront=None) -> dict

Returns:
    {"revenue": int, "currency": "USD", "source": "claude_web_search", "confidence": "high|medium|low"}
    or
    {"revenue": None, "error": "not_found"}
"""

import logging
import re
from typing import Optional

import anthropic

import config

logger = logging.getLogger(__name__)

_SYSTEM = """You are a revenue research assistant. Given a company name, website domain,
and optionally an Amazon storefront URL, find their most recent annual revenue
(trailing 12 months or last fiscal year).

IMPORTANT: Try ALL of these search strategies in order, stopping when you find data:
1. Search: "<domain> revenue" (e.g. "ilctech.com revenue")
2. Search: "<company_name> annual revenue"
3. Search: "<company_name> <domain> revenue"
4. Search: "<company_name> sales revenue site:crunchbase.com OR site:zoominfo.com OR site:dnb.com"
5. If Amazon storefront provided, search: "<amazon_store_name> revenue"

Search these sources in order — do not stop after one search:
1. Crunchbase, ZoomInfo, D&B Hoovers (dnb.com) — most reliable
2. SimilarWeb, Semrush, Owler, Craft.co, Manta — traffic/revenue estimates
3. Press releases, SEC filings, LinkedIn, Forbes, Bloomberg
4. Any site that mentions the company + revenue in the same context

Revenue estimates from data aggregators (ZoomInfo, D&B, Owler, SimilarWeb) are acceptable.
For small companies, Owler and Craft.co often have estimates even when others don't.

Respond ONLY with a JSON object, no markdown, no explanation:
{
  "revenue_usd": <integer dollars or null>,
  "confidence": "high|medium|low",
  "source_description": "<where you found it and what query worked>"
}

Rules:
- revenue_usd must be an integer (round to nearest dollar)
- If revenue is in another currency, convert to USD
- If you find a range, use the midpoint
- confidence=high means verified from official filing/press release
- confidence=medium means reliable third-party source (Crunchbase, ZoomInfo, D&B)
- confidence=low means rough estimate or indirect inference
- Only return null if ALL search strategies above return nothing useful
"""


def _parse_response(text: str) -> Optional[dict]:
    """Extract JSON from Claude's response."""
    import json
    # Strip markdown code fences if present
    text = re.sub(r"```(?:json)?\s*", "", text).strip()
    try:
        data = json.loads(text)
        return data
    except Exception:
        # Try to extract JSON object
        m = re.search(r'\{[^{}]+\}', text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except Exception:
                pass
    return None


def get_revenue_via_web(
    company_name: str,
    domain: str,
    amazon_storefront: str = "",
) -> dict:
    """Use Claude with web search to find annual revenue from public sources."""
    logger.info("Claude web search — company='%s' domain='%s'", company_name, domain)

    if not config.ANTHROPIC_API_KEY:
        logger.warning("ANTHROPIC_API_KEY not set — skipping Claude web search")
        return {"revenue": None, "error": "not_configured"}

    clean_domain = ""
    if domain:
        clean_domain = domain.lower().replace("https://", "").replace("http://", "").split("/")[0]

    queries = []
    if clean_domain:
        queries += [
            f"{clean_domain} site:zoominfo.com",
            f"{clean_domain} site:crunchbase.com",
            f"{clean_domain} site:dnb.com",
            f"{clean_domain} site:owler.com",
            f"{clean_domain} revenue",
        ]
    queries += [
        f'"{company_name}" site:zoominfo.com',
        f'"{company_name}" annual revenue',
        f'"{company_name}" revenue',
    ]
    if amazon_storefront:
        queries.append(f"{amazon_storefront} revenue")

    user_message = (
        f"Company name: {company_name}\n"
        f"Website domain: {clean_domain or 'unknown'}\n\n"
        f"Run these searches IN ORDER and stop as soon as you find a revenue figure. "
        f"For each search, visit the actual page (not just the snippet) to extract the number:\n\n"
        + "\n".join(f"{i+1}. {q}" for i, q in enumerate(queries))
        + "\n\nReturn the revenue in USD as an integer."
    )

    try:
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=_SYSTEM,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content": user_message}],
        )

        # Extract text from final response
        text = ""
        for block in response.content:
            if hasattr(block, "text"):
                text += block.text

        logger.debug("Claude web search raw response: %s", text)

        parsed = _parse_response(text)
        if not parsed:
            logger.warning("Claude web search: could not parse response for '%s'", company_name)
            return {"revenue": None, "error": "parse_error"}

        revenue = parsed.get("revenue_usd")
        if revenue is None:
            logger.info("Claude web search: no revenue found for '%s'", company_name)
            return {"revenue": None, "error": "not_found"}

        revenue = round(int(revenue))
        confidence = parsed.get("confidence", "low")
        logger.info("Claude web search hit: %s → $%d (confidence=%s)", company_name, revenue, confidence)

        return {
            "revenue": revenue,
            "currency": "USD",
            "source": "claude_web_search",
            "confidence": confidence,
            "source_description": parsed.get("source_description", ""),
        }

    except Exception as exc:
        logger.warning("Claude web search error (%s): %s", company_name, exc)
        return {"revenue": None, "error": "api_error", "message": str(exc)}
