"""Shared pytest fixtures and failure-context logging.

Every test runs under a unique ``run_id`` that is attached to all log
records and printed in failure headers, so a failing reproduction can be
correlated with the exact test input (fixture file / fingerprint) and
engine version.
"""
from __future__ import annotations

import logging
import sys
import uuid
from pathlib import Path

import pytest

from app import __version__ as engine_version
from app.config import runtime_versions
from app.rules.loader import load_file
from app.storage import connect, init_schema, EvidenceStore
from app.api.app import create_app
from app.config import Settings

FIXTURES = Path(__file__).parent / "fixtures"


class _RunIdFilter(logging.Filter):
    def __init__(self) -> None:
        super().__init__()
        self.run_id = "-"

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = self.run_id
        return True


_RUN_FILTER = _RunIdFilter()


@pytest.fixture(scope="session", autouse=True)
def _configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s run=%(run_id)s | %(message)s")
    )
    handler.addFilter(_RUN_FILTER)
    root = logging.getLogger("temporal-planner")
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    root.propagate = False


def pytest_configure(config: pytest.Config) -> None:
    versions = runtime_versions()
    print(
        "\n[conftest] test session identity: "
        f"engine={engine_version} python={versions['python']} "
        f"fastapi={versions['fastapi']} pydantic={versions['pydantic']}",
        file=sys.stdout,
    )


@pytest.fixture
def run_id() -> str:
    ident = f"test-{uuid.uuid4().hex[:12]}"
    _RUN_FILTER.run_id = ident
    logging.getLogger("temporal-planner").info("test run identity %s started", ident)
    return ident


@pytest.fixture
def fixture_dir() -> Path:
    return FIXTURES


@pytest.fixture
def drone_problem(run_id: str):
    path = FIXTURES / "drone_delivery.yaml"
    problem = load_file(path)
    logging.getLogger("temporal-planner").info(
        "run_id=%s loaded fixture %s problem=%r horizon=%s actions=%d",
        run_id, path, problem.name, problem.horizon, len(problem.actions),
    )
    return problem


@pytest.fixture
def workshop_problem(run_id: str):
    path = FIXTURES / "workshop.yaml"
    problem = load_file(path)
    logging.getLogger("temporal-planner").info(
        "run_id=%s loaded fixture %s problem=%r horizon=%s actions=%d",
        run_id, path, problem.name, problem.horizon, len(problem.actions),
    )
    return problem


@pytest.fixture
def reactor_problem(run_id: str):
    path = FIXTURES / "reactor.yaml"
    problem = load_file(path)
    logging.getLogger("temporal-planner").info(
        "run_id=%s loaded fixture %s problem=%r horizon=%s actions=%d",
        run_id, path, problem.name, problem.horizon, len(problem.actions),
    )
    return problem


@pytest.fixture
def store(tmp_path: Path) -> EvidenceStore:
    conn = connect(tmp_path / "evidence.db")
    init_schema(conn)
    return EvidenceStore(conn)


@pytest.fixture
def client(tmp_path: Path, store: EvidenceStore):
    from fastapi.testclient import TestClient

    settings = Settings(db_path=str(tmp_path / "api.db"))
    application = create_app(settings, store=store)
    with TestClient(application) as test_client:
        yield test_client


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if call.when == "call" and report.failed:
        # Surface the correlation identity right where pytest prints the
        # failure, alongside versions and the parametrize/fixture identity.
        versions = runtime_versions()
        report.sections.append(
            (
                "reproduction context",
                "\n".join(
                    [
                        f"test_node: {item.nodeid}",
                        f"run_id: {_RUN_FILTER.run_id}",
                        f"engine_version: {engine_version}",
                        f"versions: python={versions['python']} fastapi={versions['fastapi']} "
                        f"pydantic={versions['pydantic']}",
                        f"failure_phase: {call.when}",
                    ]
                ),
            )
        )
