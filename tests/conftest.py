"""Shared pytest configuration.

Every test gets a unique run identity (derived from the test node) in its
log records, so ``reports/pytest.log`` lines can be correlated back to the
exact test/input that produced them.

The application logger uses ``propagate=False`` (see
``logging_ctx.configure_logging``), so pytest's own log-file capture would
not see its records; we attach a dedicated FileHandler here once per session.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from toeplitz_fft.logging_ctx import configure_logging, set_run_id

_FILE_HANDLER_ATTACHED = False


@pytest.fixture(autouse=True)
def _test_run_id(request: pytest.FixtureRequest) -> None:
    global _FILE_HANDLER_ATTACHED
    log = configure_logging("INFO")
    if not _FILE_HANDLER_ATTACHED:
        log_path = Path("reports/pytest.log")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s [run=%(run_id)s] %(message)s"))
        from toeplitz_fft.logging_ctx import _RunIdFilter  # noqa: PLC2701

        handler.addFilter(_RunIdFilter())
        log.addHandler(handler)
        _FILE_HANDLER_ATTACHED = True
    set_run_id("test-" + request.node.name.replace(" ", "_")[:48])
