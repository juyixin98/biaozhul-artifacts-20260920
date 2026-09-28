"""End-to-end verification: every fixture against the independent enumerator."""
from __future__ import annotations

import pytest

from csp_service.config import Settings
from csp_service.runlog import RunLogger
from scripts.verify import verify_fixture


def test_all_fixtures_pass_verification(fixtures_dir, tmp_path, test_run_logger):
    settings = Settings(db_path=str(tmp_path / "e.db"), log_dir=str(tmp_path))
    logger = RunLogger(tmp_path)
    failures = []
    for path in sorted(fixtures_dir.glob("*.json")):
        ok = verify_fixture(path, settings, logger)
        test_run_logger.log("fixture_verified", fixture=path.stem,
                            verdict="pass" if ok else "FAIL",
                            basis="scripts.verify.verify_fixture",
                            detail_log=str(logger.path))
        if not ok:
            failures.append(path.name)
    assert not failures, f"fixtures failed verification: {failures}"


@pytest.mark.parametrize("fixture_name", [
    "hall_conflict", "isolated_variable", "multi_solution", "deep_backtrack",
])
def test_fixture_individually(fixture_name, fixtures_dir, tmp_path):
    settings = Settings(db_path=str(tmp_path / "e.db"), log_dir=str(tmp_path))
    logger = RunLogger(tmp_path)
    assert verify_fixture(fixtures_dir / f"{fixture_name}.json", settings, logger)
