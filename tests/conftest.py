"""Shared pytest configuration.

Every test session writes a log file named with a unique run id and the
engine versions, so a failure can always be correlated with:

* the session/run identity and the exact input fingerprint,
* the service / Python / dependency versions,
* the progress and decision steps (search trace) and the verdict basis.
"""

from __future__ import annotations

import json
import logging
import platform
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tplan import __version__
from tplan.config import Settings
from tplan.service import PlanningService
from tplan.store import EvidenceStore

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
SESSION_RUN_ID = f"test-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
LOG_FILE = LOG_DIR / f"{SESSION_RUN_ID}.log"

_handler = logging.FileHandler(LOG_FILE, mode="w", encoding="utf-8")
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
_root = logging.getLogger("tplan")
_root.setLevel(logging.DEBUG)
_root.addHandler(_handler)
_root.propagate = False


def _versions() -> dict[str, str]:
    import fastapi
    import pydantic
    import pytest as _pytest

    return {
        "session_run_id": SESSION_RUN_ID,
        "service_version": __version__,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "pytest": _pytest.__version__,
        "fastapi": fastapi.__version__,
        "pydantic": pydantic.VERSION,
        "executable": sys.executable,
    }


VERSIONS = _versions()
_root.info("session start %s", json.dumps(VERSIONS, sort_keys=True))
print(f"\n[tplan] session_run_id={SESSION_RUN_ID} log={LOG_FILE}")


@pytest.fixture(scope="session")
def session_run_id() -> str:
    return SESSION_RUN_ID


@pytest.fixture(scope="session")
def versions() -> dict[str, str]:
    return dict(VERSIONS)


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=str(tmp_path / "evidence.sqlite3"),
        default_node_budget=100_000,
        default_time_budget_seconds=10.0,
        max_horizon=40,
        max_actions=32,
        max_repeats=4,
        log_dir=str(tmp_path / "logs"),
    )


@pytest.fixture()
def service(settings: Settings) -> PlanningService:
    svc = PlanningService(settings, EvidenceStore(settings.db_path))
    yield svc
    svc.close()


@pytest.fixture()
def fixture_dir() -> Path:
    return Path(__file__).resolve().parent / "fixtures"


@pytest.fixture()
def load_fixture(fixture_dir: Path):
    def _load(name: str) -> dict:
        with (fixture_dir / name).open(encoding="utf-8") as fh:
            return json.load(fh)

    return _load
