"""Tests for the SQLite evidence store and the service orchestration."""
from __future__ import annotations

from pathlib import Path

import pytest

from htn_planner.config import Settings
from htn_planner.models import Problem
from htn_planner.service import PlanningService, ServiceError
from htn_planner.storage import EvidenceStore

pytestmark = pytest.mark.integration

from .fixture_loader import PROBLEM_DIR


@pytest.fixture
def service(tmp_path: Path) -> PlanningService:
    fixture_root = PROBLEM_DIR.parent
    settings = Settings(
        db_path=str(tmp_path / "test.db"),
        fixture_dir=str(fixture_root),
        domain_dir=str(fixture_root / "domains"),
        log_level="INFO",
        service_name="finite-htn-planner",
        service_version="1.0.0",
    )
    return PlanningService(settings=settings, store=EvidenceStore(settings.db_path))


def test_feasible_run_is_persisted_and_correlated_by_request_id(service) -> None:
    result = service.plan_from_fixture(
        "logistics_direct.yaml", request_id="req-direct-1"
    )
    assert result.request_id == "req-direct-1"
    summary = service.store.get_summary("req-direct-1")
    assert summary is not None
    assert summary["feasible"] is True
    assert summary["terminal_failure"] is None
    assert summary["domain"] == "logistics"
    assert summary["domain_version"] == "1.0.0"
    tree = service.store.get_tree("req-direct-1")
    assert tree["roots"] == result.roots
    assert len(tree["nodes"]) == len(result.nodes)


def test_failure_run_records_terminal_category_and_branch_evidence(service) -> None:
    result = service.plan_from_fixture(
        "logistics_no_viable.yaml", request_id="req-fail-1"
    )
    assert result.feasible is False
    evidence = service.store.get_failures("req-fail-1")
    terminal = [e for e in evidence if e["branch"] == "terminal"]
    assert terminal[-1]["kind"] == "no_viable_method"
    assert set(terminal[-1]["tried_methods"]) == {
        "m-haul-direct", "m-haul-via-hub"
    }


def test_abandoned_branches_stored_separately_from_terminal_failures(service) -> None:
    result = service.plan_from_fixture(
        "logistics_via_hub.yaml", request_id="req-via-1"
    )
    assert result.feasible is True
    evidence = service.store.get_failures("req-via-1")
    assert all(e["branch"] == "abandoned" for e in evidence)
    assert any(e["method"] == "m-haul-direct" for e in evidence)


def test_audit_log_records_key_steps_locations_and_identity(service) -> None:
    service.plan_from_fixture("logistics_direct.yaml", request_id="req-audit-1")
    audit = service.store.get_audit("req-audit-1")
    messages = [a["message"] for a in audit]
    locations = {a["location"] for a in audit}
    assert any("received request_id=req-audit-1" in m for m in messages)
    assert any("FEASIBLE" in m and "req-audit-1" in m for m in messages)
    assert "service:receive" in locations and "service:respond" in locations
    assert any(loc.startswith("kernel:") for loc in locations)
    # Steps are ordered and each entry carries a location and timestamp.
    assert audit == sorted(audit, key=lambda a: a["step"])
    assert all(a["at"] for a in audit)


def test_uncertainty_is_separated_and_not_marked_passed(service) -> None:
    result = service.plan_from_fixture(
        "logistics_via_hub.yaml", request_id="req-unc-1"
    )
    assert result.feasible is True
    assert result.uncertainty  # non-empty: backtracking alternatives existed
    assert all("abandoned" in u for u in result.uncertainty)


def test_unknown_fixture_raises_service_error(service) -> None:
    with pytest.raises(ServiceError, match="unknown problem fixture"):
        service.plan_from_fixture("does_not_exist.yaml")


def test_inline_plan_round_trips(service) -> None:
    problem = Problem(
        name="inline", domain="logistics",
        initial_facts=[["token_ready"]],
        goal_task="two_consume", goal_args=["a", "b"],
    )
    result = service.plan_inline(problem, request_id="req-inline-1")
    assert result.feasible is False
    assert service.store.get_summary("req-inline-1")["terminal_failure"] == (
        "no_viable_method"
    )


def test_listing_plans_orders_most_recent_first(service) -> None:
    service.plan_from_fixture("logistics_direct.yaml", request_id="req-a")
    service.plan_from_fixture("logistics_via_hub.yaml", request_id="req-b")
    ids = [p["request_id"] for p in service.store.list_plans()]
    assert ids[0] == "req-b" and "req-a" in ids
