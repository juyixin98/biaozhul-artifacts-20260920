"""Application configuration, sourced from environment variables."""
from __future__ import annotations

import os


class Settings:
    DATABASE_URL: str = os.environ.get(
        "DATABASE_URL",
        "postgresql+psycopg://skillpulse:skillpulse@localhost:5432/skillpulse",
    )
    SEAT_HOLD_HOURS: int = int(os.environ.get("SEAT_HOLD_HOURS", "48"))
    SEED_DEMO: bool = os.environ.get("SEED_DEMO", "0") == "1"


settings = Settings()
