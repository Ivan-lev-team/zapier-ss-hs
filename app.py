"""
Flask API — receives POST /scrape from Zapier, returns T12M Amazon revenue.
"""

import logging
import time
from flask import Flask, request, jsonify

import config
from scraper.smartscout import get_t12m_revenue

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
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.post("/scrape")
def scrape():
    """
    Expected JSON body:
        {"company_name": "Acme Corp", "domain": "acme.com"}

    Response (success):
        {"revenue": 1234567.89, "currency": "USD", "source": "smartscout",
         "company_name": "Acme Corp", "query_used": "Acme Corp"}

    Response (not found / error):
        {"revenue": null, "error": "not_found", "company_name": "Acme Corp"}
    """
    body = request.get_json(silent=True) or {}

    company_name = (body.get("company_name") or "").strip()
    domain = (body.get("domain") or "").strip()

    if not company_name and not domain:
        return jsonify({"error": "missing_fields",
                        "message": "Provide at least company_name or domain"}), 400

    # Use domain as fallback name if company_name not supplied
    if not company_name:
        company_name = domain

    start = time.monotonic()
    result = get_t12m_revenue(company_name, domain)
    elapsed = time.monotonic() - start

    result["elapsed_seconds"] = round(elapsed, 2)

    status_code = 200 if result.get("revenue") is not None else 200
    return jsonify(result), status_code


# ---------------------------------------------------------------------------
# Dev entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=config.PORT, debug=False)
