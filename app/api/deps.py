"""Shared dependencies."""
from __future__ import annotations

from functools import lru_cache

from ..config import Settings
from ..services import ReasoningService
from ..store import EvidenceStore


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()


@lru_cache(maxsize=1)
def get_store() -> EvidenceStore:
    return EvidenceStore(get_settings().db_path)


@lru_cache(maxsize=1)
def get_service() -> ReasoningService:
    settings = get_settings()
    return ReasoningService(get_store(), model_cap=settings.model_cap)
