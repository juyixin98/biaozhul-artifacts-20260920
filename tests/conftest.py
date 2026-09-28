from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from csp_service.runlog import RunLogger  # noqa: E402

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


@pytest.fixture(scope="session")
def test_run_logger():
    """One JSONL log per test session; every test outcome is recorded with
    the session run_id so logs can be traced back to inputs and versions."""
    logger = RunLogger(Path("logs"), run_id=None)
    yield logger
    logger.log("test_session_done")


@pytest.fixture(autouse=True)
def _log_test_outcome(request, test_run_logger):
    test_run_logger.log("test_start", test=request.node.nodeid)
    yield
    report = getattr(request.node, "_report", None)
    outcome = report.outcome if report else "unknown"
    test_run_logger.log("test_done", test=request.node.nodeid, verdict=outcome)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.when == "call":
        item._report = report


@pytest.fixture()
def fixtures_dir() -> Path:
    return FIXTURES_DIR


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / f"{name}.json").read_text())


@pytest.fixture()
def fixture_loader():
    return load_fixture
