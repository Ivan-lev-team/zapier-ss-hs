"""
Flask API — receives POST /scrape from Zapier, returns T12M Amazon revenue.

Sync mode:  no zapier_callback_url → waits and returns result directly.
Async mode: zapier_callback_url provided → returns {"status": "processing"}
            immediately, scrapes in background, POSTs result to callback URL.
"""

import logging
import threading
import time

import requests
from flask import Flask, request, jsonify

import config
from scraper.smartscout import get_t12m_revenue
from scraper.storeleads import get_shopify_revenue
from scraper.leadmagic import get_company_revenue
from scraper.claude_search import get_revenue_via_web

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.secret_key = config.FLASK_SECRET_KEY


@app.before_request
def _log_request():
    logger.info("→ %s %s  body=%s", request.method, request.path,
                request.get_json(silent=True))


@app.after_request
def _log_response(response):
    logger.info("← %s %s  status=%d", request.method, request.path, response.status_code)
    return response


# ---------------------------------------------------------------------------
# Callback helper
# ---------------------------------------------------------------------------

def _post_callback(callback_url: str, payload: dict) -> None:
    """POST result to Zapier catch-hook URL, retrying up to 3 times."""
    for attempt in range(1, 4):
        try:
            resp = requests.post(callback_url, json=payload, timeout=15)
            resp.raise_for_status()
            logger.info("Callback delivered (attempt %d) — status %d", attempt, resp.status_code)
            return
        except Exception as exc:
            logger.warning("Callback attempt %d failed: %s", attempt, exc)
            if attempt < 3:
                time.sleep(attempt * 2)  # 2s, 4s
    logger.error("All callback attempts failed for URL: %s", callback_url)


def _lookup_revenue(company_name: str, domain: str) -> dict:
    """
    Waterfall revenue lookup:
      1. SmartScout  — Amazon T12M revenue
      2. StorLeads   — Shopify store revenue
      3. LeadMagic   — B2B company database
      4. Claude web  — Google search across reliable public sources
    Returns first result with a non-None revenue.
    """
    # 1. SmartScout
    result = get_t12m_revenue(company_name, domain)
    if result.get("revenue") is not None:
        return result
    logger.info("SmartScout: not found for '%s' — trying StorLeads", company_name)

    # 2. StorLeads (Shopify)
    result = get_shopify_revenue(company_name, domain)
    if result.get("revenue") is not None:
        return result
    logger.info("StorLeads: not found for '%s' — trying LeadMagic", company_name)

    # 3. LeadMagic
    result = get_company_revenue(company_name, domain)
    if result.get("revenue") is not None:
        return result
    logger.info("LeadMagic: not found for '%s' — trying Claude web search", company_name)

    # 4. Claude web search (last resort)
    result = get_revenue_via_web(company_name, domain)
    if result.get("revenue") is not None:
        return result

    logger.info("All sources exhausted for '%s'", company_name)
    return {"revenue": None, "error": "not_found", "source": "all"}


def _scrape_and_callback(
    company_name: str,
    domain: str,
    callback_url: str,
    passthrough: dict,
) -> None:
    """Run in a background thread: scrape then POST result to Zapier."""
    result = _lookup_revenue(company_name, domain)
    payload = {**result, **passthrough}
    _post_callback(callback_url, payload)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.post("/scrape")
def scrape():
    """
    Expected JSON body:
        {
            "company_name": "Acme Corp",     # required (or domain)
            "domain":       "acme.com",      # required (or company_name)
            "zapier_callback_url": "https://hooks.zapier.com/...",  # optional
            "deal_id":      "123",           # optional, passed through to callback
            "company_id":   "456"            # optional, passed through to callback
        }

    Sync response (no zapier_callback_url):
        {"revenue": 1234567.89, "currency": "USD", "source": "smartscout", ...}

    Async response (zapier_callback_url provided):
        {"status": "processing", "company_name": "Acme Corp"}
        → result POSTed to zapier_callback_url when ready
    """
    body = request.get_json(silent=True) or {}

    company_name = (body.get("company_name") or "").strip()
    domain = (body.get("domain") or "").strip()
    callback_url = (body.get("zapier_callback_url") or "").strip()

    if not company_name and not domain:
        return jsonify({
            "error": "missing_fields",
            "message": "Provide at least company_name or domain",
        }), 400

    if not company_name:
        company_name = domain

    # Fields to pass through unchanged in the callback payload
    passthrough = {k: body[k] for k in ("deal_id", "company_id") if k in body}

    if callback_url:
        # Async mode — respond immediately, scrape in background
        thread = threading.Thread(
            target=_scrape_and_callback,
            args=(company_name, domain, callback_url, passthrough),
            daemon=True,
        )
        thread.start()
        logger.info("Async scrape started for '%s' (callback: %s)", company_name, callback_url)
        return jsonify({"status": "processing", "company_name": company_name})

    # Sync mode — wait for result and return directly (for testing)
    start = time.monotonic()
    result = _lookup_revenue(company_name, domain)
    result["elapsed_seconds"] = round(time.monotonic() - start, 2)
    if passthrough:
        result.update(passthrough)
    return jsonify(result)


# ---------------------------------------------------------------------------
# Dev entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=config.PORT, debug=False)
