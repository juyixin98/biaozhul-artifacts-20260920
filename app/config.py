import os

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg://skillpulse:skillpulse@localhost:5432/skillpulse",
)

# How long a pending enrollment holds its seat before it is released.
SEAT_CONFIRM_TTL_HOURS = int(os.getenv("SEAT_CONFIRM_TTL_HOURS", "48"))

# Maximum number of steps allowed in one program version.
MAX_STEPS_PER_VERSION = 50

# Seed demo data on startup (used by docker-compose).
SEED_DEMO = os.getenv("SEED_DEMO", "false").lower() in ("1", "true", "yes")
