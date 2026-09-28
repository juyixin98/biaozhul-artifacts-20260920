"""Test configuration: run identity, versions and verdict logging.

Every test session writes a log file ``tests/logs/test_run_<run_id>.log`` that
records the run id, library versions, per-test input identity, progress and
the basis of each verdict. Logs correlate to inputs via model names/fixture
names and to the session via the run id.
"""

from __future__ import annotations

import importlib.metadata
import logging
import os
import platform
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import __version__ as solver_version  # noqa: E402

RUN_ID = uuid.uuid4().hex
LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_PATH = LOG_DIR / f"test_run_{RUN_ID}.log"


def _configure_logging() -> logging.Logger:
    logger = logging.getLogger("csp.tests")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    formatter = logging.Formatter(
        f"%(asctime)s run={RUN_ID} %(levelname)s %(message)s"
    )
    file_handler = logging.FileHandler(LOG_PATH, mode="w", encoding="utf-8")
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    stream_handler.setLevel(logging.INFO)
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    logger.propagate = False
    return logger


LOGGER = _configure_logging()


def log_verdict(test_name: str, basis: str, **details) -> None:
    """Record the concrete basis for a test verdict."""
    rendered = " ".join(f"{key}={value}" for key, value in details.items())
    LOGGER.info("VERDICT test=%s basis=%s %s", test_name, basis, rendered)


@pytest.fixture(scope="session", autouse=True)
def session_identity() -> str:
    versions = {
        "python": platform.python_version(),
        "solver": solver_version,
        "fastapi": importlib.metadata.version("fastapi"),
        "pydantic": importlib.metadata.version("pydantic"),
        "pytest": importlib.metadata.version("pytest"),
    }
    LOGGER.info(
        "SESSION start run_id=%s versions=%s platform=%s",
        RUN_ID,
        versions,
        platform.platform(),
    )
    yield RUN_ID
    LOGGER.info("SESSION end run_id=%s log=%s", RUN_ID, LOG_PATH)


@pytest.fixture(autouse=True)
def _log_test_boundaries(request):
    LOGGER.info("TEST start node=%s", request.node.nodeid)
    yield
    rep_call = getattr(request.node, "rep_call", None)
    outcome = rep_call.failed if rep_call is not None else False
    LOGGER.info(
        "TEST end node=%s outcome=%s",
        request.node.nodeid,
        "failed" if outcome else "finished",
    )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    item.rep_call = report if report.when == "call" else getattr(item, "rep_call", report)
