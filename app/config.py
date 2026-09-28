"""Application configuration.

All tunables live here so tests and scripts can override them explicitly
instead of relying on hidden globals.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """Runtime settings for the audit backend.

    Attributes:
        db_path: SQLite database file. Use ``":memory:"`` in tests.
        small_sample_threshold: corpora with fewer transactions than this
            trigger a ``SMALL_SAMPLE`` warning on every rule result.
        rare_event_count_threshold: rules whose joint support *count* is
            below this trigger a ``RARE_EVENT`` warning.
        ubiquitous_support_threshold: consequents at or above this support
            ratio trigger a ``UBIQUITOUS_CONSEQUENT`` warning (high
            confidence is then an artifact of prevalence, not association).
        mask_items_in_logs: when True, item names are hashed before being
            written to logs (items may be sensitive, e.g. medical codes).
    """

    db_path: str = "audit.db"
    small_sample_threshold: int = 30
    rare_event_count_threshold: int = 5
    ubiquitous_support_threshold: float = 0.9
    mask_items_in_logs: bool = True


def get_settings() -> Settings:
    """Return the default settings.

    Kept as a function (not a module-level singleton) so the FastAPI app can
    override it via dependency injection in tests.
    """
    return Settings()
