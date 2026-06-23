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

_SYSTEM = """You are a revenue research assistant. Given a company name and domain,
find their most recent annual revenue (trailing 12 months or last fiscal year).

Search Google for reliable sources: company press releases, Crunchbase, LinkedIn,
Forbes, Bloomberg, SEC filings, or industry reports. Prefer verified data over estimates.

Respond ONLY with a JSON object, no markdown, no explanation:
{
  "revenue_usd": <integer dollars or null>,
  "confidence": "high|medium|low",
  "source_description": "<where you found it>"
}

Rules:
- revenue_usd must be an integer (round to nearest dollar)
- If revenue is in another currency, convert to USD
- If you find a range, use the midpoint
- confidence=high means verified from official filing/press release
- confidence=medium means reliable third-party source (Crunchbase, Bloomberg)
- confidence=low means estimate or indirect inference
- Return null if you genuinely cannot find anything reliable
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

    context_parts = [f"Company name: {company_name}"]
    if domain:
        context_parts.append(f"Website: {domain}")
    if amazon_storefront:
        context_parts.append(f"Amazon storefront: {amazon_storefront}")
    context_parts.append("Find their trailing 12-month or most recent annual revenue in USD.")

    user_message = "\n".join(context_parts)

    try:
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

        response = client.messages.create(
            model="claude-opus-4-6",
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
