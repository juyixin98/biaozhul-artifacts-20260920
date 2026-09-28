"""Shared pytest helpers."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.api.logging_setup import configure_logger  # noqa: E402
from app.data.ledger import Ledger  # noqa: E402


@pytest.fixture
def isolated_runtime(monkeypatch, tmp_path):
    """Point the ledger and structured logger at a temp directory for tests."""
    ledger = Ledger(tmp_path / "ledger.db")
    logger = configure_logger(tmp_path / "service.log", "DEBUG")

    import app.main as main

    main.get_ledger.cache_clear()
    main.get_logger.cache_clear()
    monkeypatch.setattr(main, "get_ledger", lambda: ledger)
    monkeypatch.setattr(main, "get_logger", lambda: logger)
    yield {"ledger": ledger, "log_path": tmp_path / "service.log", "tmp": tmp_path}
