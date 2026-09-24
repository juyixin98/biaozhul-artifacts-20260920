"""Global configuration and physical defaults."""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("DISPATCH_DB", BASE_DIR / "dispatch.db"))

# Secret used for HMAC signed tokens. A development default exists so the app
# boots out of the box, but deployments must set DISPATCH_SECRET.
SECRET_KEY = os.environ.get(
    "DISPATCH_SECRET", "dev-only-insecure-secret-change-me-please"
)
TOKEN_TTL_SECONDS = int(os.environ.get("DISPATCH_TOKEN_TTL", "28800"))

# --- Energy model defaults (see README §5) ---
BASE_CONSUMPTION_KWH_PER_M = 0.020
PAYLOAD_FACTOR_KWH_PER_M_KG = 0.0008
IDLE_KWH_PER_MINUTE = 0.05

# A robot must retain this fraction of its battery capacity as headroom after
# arriving at a charger: required = mission + margin.
SAFETY_MARGIN_FRACTION = 0.10

# --- Cost model: everything converted to a monetary-ish unit so the operator
# sees an explainable total instead of a magic number ---
ENERGY_PRICE_PER_KWH = 1.0
DISTANCE_PRICE_PER_M = 0.02
WAIT_PRICE_PER_MIN = 0.6
PRIORITY_BONUS = 5.0
MAX_TASK_PRIORITY = 10

# Charger is considered reachable only from robots that need at most this many
# concurrent robots heading there (each charger has its own capacity).
DEFAULT_CHARGER_CAPACITY = 1

# PBKDF2 parameters
PBKDF2_ITERATIONS = 240_000
PBKDF2_SALT_BYTES = 16

# Telemetry deviation thresholds (relative)
MEASURED_CRITICAL_RATIO = 0.90  # <90% of predicted SoC => critical, block start
MEASURED_WARN_RATIO = 0.98      # <98% => warning

# Seeded demo operator
DEMO_OPERATOR = ("operator", "demo-password-123")
