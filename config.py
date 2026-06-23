import os
import tempfile
from dotenv import load_dotenv

load_dotenv()

SS_EMAIL = os.environ["SS_EMAIL"]
SS_PASSWORD = os.environ["SS_PASSWORD"]
ZENROWS_API_KEY = os.getenv("ZENROWS_API_KEY", "")
USE_ZENROWS = os.getenv("USE_ZENROWS", "false").lower() == "true"
STORELEADS_API_KEY = os.getenv("STORELEADS_API_KEY", "8c15e821-6267-4f50-6fb0-b121ea5c")
LEADMAGIC_API_KEY = os.getenv("LEADMAGIC_API_KEY", "lm_live_e4a6a68db5e12b9806cda6867f3b1ee715b6e373")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-me")
PORT = int(os.getenv("PORT", "5055"))

SMARTSCOUT_BASE_URL = "https://app.smartscout.com"
SESSION_CACHE_PATH = os.getenv("SESSION_CACHE_PATH", "/tmp/ss_cache/session.json")
SESSION_MAX_AGE_HOURS = 8
REQUEST_TIMEOUT_MS = 30_000
