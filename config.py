import os
from dotenv import load_dotenv

load_dotenv()

SS_EMAIL = os.environ["SS_EMAIL"]
SS_PASSWORD = os.environ["SS_PASSWORD"]
ZENROWS_API_KEY = os.getenv("ZENROWS_API_KEY", "")
USE_ZENROWS = os.getenv("USE_ZENROWS", "false").lower() == "true"
FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-me")
PORT = int(os.getenv("PORT", "5055"))

SMARTSCOUT_BASE_URL = "https://app.smartscout.com"
SESSION_CACHE_PATH = "/tmp/ss_session.json"
SESSION_MAX_AGE_HOURS = 8
REQUEST_TIMEOUT_MS = 30_000
