"""Application configuration.

Configuration is read from environment variables so the same image runs in
Docker, CI and locally. ``DATABASE_URL`` is the only required setting.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _split_origins(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    database_url: str = field(
        default_factory=lambda: os.getenv(
            "DATABASE_URL",
            "postgresql+psycopg2://careforce:careforce@localhost:5432/careforce",
        )
    )
    cors_origins: list[str] = field(
        default_factory=lambda: _split_origins(os.getenv("CORS_ORIGINS", ""))
    )
    # How long a worker has to accept an assignment invitation.
    invitation_ttl_minutes: int = field(
        default_factory=lambda: int(os.getenv("INVITATION_TTL_MINUTES", "8"))
    )
    # Generated task horizon.
    horizon_days: int = field(
        default_factory=lambda: int(os.getenv("HORIZON_DAYS", "14"))
    )
    # Hard scheduling rules from the domain specification.
    weekly_hour_limit: int = field(
        default_factory=lambda: int(os.getenv("WEEKLY_HOUR_LIMIT", "44"))
    )
    rest_between_shifts_hours: int = field(
        default_factory=lambda: int(os.getenv("REST_BETWEEN_SHIFTS_HOURS", "10"))
    )


settings = Settings()
