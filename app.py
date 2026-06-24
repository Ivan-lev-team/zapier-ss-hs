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
from scraper.google_snippet import get_revenue_from_snippets
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


# Source registry — name → callable(company_name, domain) -> dict
# Used for isolated single-source testing via the "source" param.
_SOURCES = [
    ("smartscout", lambda c, d: get_t12m_revenue(c, d)),
    ("storeleads", lambda c, d: get_shopify_revenue(c, d)),
    ("leadmagic",  lambda c, d: get_company_revenue(c, d)),
    ("google",     lambda c, d: get_revenue_from_snippets(c, d)),
    ("claude",     lambda c, d: get_revenue_via_web(c, d)),
]
_SOURCE_MAP = dict(_SOURCES)

# General-revenue fallbacks (total company revenue, not channel-specific).
# Only consulted when BOTH channel sources come up empty.
_FALLBACK_SOURCES = [
    ("leadmagic", lambda c, d: get_company_revenue(c, d)),
    ("google",    lambda c, d: get_revenue_from_snippets(c, d)),
    ("claude",    lambda c, d: get_revenue_via_web(c, d)),
]


def _run_single_source(source: str, company_name: str, domain: str) -> dict:
    """Run exactly one named source (for isolated testing/debugging)."""
    fn = _SOURCE_MAP.get(source)
    if fn is None:
        return {"revenue": None, "error": "unknown_source",
                "message": f"Valid sources: {', '.join(_SOURCE_MAP)}"}
    logger.info("Running single source '%s' for '%s'", source, company_name)
    return fn(company_name, domain)


def _lookup_revenue(company_name: str, domain: str) -> dict:
    """
    Enrich BOTH channels independently, plus a general fallback:
      - amazon_revenue  ← SmartScout  (Amazon T12 GMV)
      - shopify_revenue ← StorLeads   (Shopify T12 sales)
      - general_revenue ← LeadMagic / Google / Claude (total company revenue),
                          consulted ONLY when both channel sources are empty.

    All values default to "" (empty string) so Zapier skips them on Update
    (an empty mapped field won't blank the HubSpot value). A brand selling on
    both channels gets both amazon_revenue and shopify_revenue filled.
    """
    out = {
        "company_name": company_name,
        "amazon_revenue": "",
        "shopify_revenue": "",
        "general_revenue": "",
        "sources": {},
    }

    # --- Amazon channel: SmartScout ---
    ss = get_t12m_revenue(company_name, domain)
    if ss.get("revenue") is not None:
        out["amazon_revenue"] = round(ss["revenue"])
        out["sources"]["amazon"] = "smartscout"
    else:
        logger.info("SmartScout: no Amazon revenue for '%s'", company_name)

    # --- Shopify channel: StorLeads ---
    sl = get_shopify_revenue(company_name, domain)
    if sl.get("revenue") is not None:
        out["shopify_revenue"] = round(sl["revenue"])
        out["sources"]["shopify"] = "storeleads"
    else:
        logger.info("StorLeads: no Shopify revenue for '%s'", company_name)

    # --- General fallback: only when BOTH channels came up empty ---
    if out["amazon_revenue"] == "" and out["shopify_revenue"] == "":
        for name, fn in _FALLBACK_SOURCES:
            r = fn(company_name, domain)
            if r.get("revenue") is not None:
                out["general_revenue"] = round(r["revenue"])
                out["sources"]["general"] = r.get("source", name)
                break
            logger.info("%s: no general revenue for '%s' — trying next", name, company_name)

    out["found"] = bool(
        out["amazon_revenue"] != ""
        or out["shopify_revenue"] != ""
        or out["general_revenue"] != ""
    )
    if not out["found"]:
        logger.info("All sources exhausted for '%s'", company_name)
        out["error"] = "not_found"

    return out


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
    source = (body.get("source") or "").strip().lower()

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
    if source:
        # Test a single source in isolation
        result = _run_single_source(source, company_name, domain)
    else:
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
