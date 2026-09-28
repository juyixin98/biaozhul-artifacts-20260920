"""Tests for SQLite evidence persistence."""

from __future__ import annotations

import pytest

from htn_planner.core.engine import Bounds, Planner
from htn_planner.lang import parse_domain, parse_problem
from htn_planner.store import EvidenceStore, StoreError
from htn_planner.verify import Verifier

DOMAIN = """
(:domain d
  (:operator (!a) (:pre (ready)) (:del (ready)) (:add (done)))
  (:method m (go) (:pre) (:tasks (!a))))
"""
PROBLEM = "(:problem p (:domain d) (:init (ready)) (:tasks (go)))"


@pytest.fixture
def planned():
    domain = parse_domain(DOMAIN)
    problem = parse_problem(PROBLEM)
    result = Planner().solve(domain, problem, "req-store")
    report = Verifier().verify(domain, problem, result)
    return domain, problem, result, report


class TestEvidenceStore:
    def test_round_trip_preserves_status_and_plan(self, planned, tmp_path) -> None:
        db = tmp_path / "evidence.db"
        store = EvidenceStore(db)
        _, _, result, report = planned
        store.save_run("req-1", DOMAIN, PROBLEM, result, "v1")
        store.save_verification("req-1", report)

        loaded = store.get_run("req-1")
        assert loaded is not None
        assert loaded["status"] == "success"
        assert loaded["engine_version"] == result.engine_version
        assert loaded["domain_version"] == "v1"
        assert loaded["result"]["plan"][0]["operator"] == "!a"
        ver = store.get_verification("req-1")
        assert ver["ok"] == 1
        assert ver["report"]["executable"] is True
        store.close()

    def test_duplicate_request_id_is_rejected(self, planned, tmp_path) -> None:
        store = EvidenceStore(tmp_path / "e.db")
        _, _, result, _ = planned
        store.save_run("req-dup", DOMAIN, PROBLEM, result, "v1")
        with pytest.raises(StoreError, match="already exists"):
            store.save_run("req-dup", DOMAIN, PROBLEM, result, "v1")
        store.close()

    def test_verification_requires_existing_run(self, planned, tmp_path) -> None:
        store = EvidenceStore(tmp_path / "e.db")
        _, _, _, report = planned
        with pytest.raises(StoreError, match="unknown request_id"):
            store.save_verification("ghost", report)
        store.close()

    def test_list_runs_filters_by_status(self, tmp_path) -> None:
        store = EvidenceStore(tmp_path / "e.db")
        domain = parse_domain(DOMAIN)

        ok_problem = parse_problem(PROBLEM)
        ok_result = Planner().solve(domain, ok_problem, "ok")
        store.save_run("ok", DOMAIN, PROBLEM, ok_result, "v")

        fail_domain = parse_domain(
            """
            (:domain f
              (:operator (!x) (:pre (need)) (:del) (:add (r)))
              (:method m (t) (:pre) (:tasks (!x))))
            """
        )
        fail_text = "(:problem fp (:domain f) (:init) (:tasks (t)))"
        fail_result = Planner().solve(fail_domain, parse_problem(fail_text), "bad")
        store.save_run("bad", "domain-src", fail_text, fail_result, "v")

        all_runs = store.list_runs()
        assert {r["request_id"] for r in all_runs} == {"ok", "bad"}
        failures = store.list_runs(status="failure")
        assert [r["request_id"] for r in failures] == ["bad"]
        store.close()

    def test_get_unknown_run_returns_none(self, tmp_path) -> None:
        store = EvidenceStore(tmp_path / "e.db")
        assert store.get_run("nope") is None
        assert store.get_verification("nope") is None
        store.close()

    def test_persisted_json_contains_failure_and_uncertain_sections(self, tmp_path) -> None:
        store = EvidenceStore(tmp_path / "e.db")
        domain = parse_domain(
            """
            (:domain f
              (:operator (!x) (:pre (need)) (:del) (:add (r)))
              (:method m (t) (:pre) (:tasks (!x))))
            """
        )
        problem = parse_problem("(:problem fp (:domain f) (:init) (:tasks (t)))")
        result = Planner().solve(domain, problem, "fr")
        store.save_run("fr", "src", "psrc", result, "v")
        loaded = store.get_run("fr")
        assert loaded["result"]["status"] == "failure"
        assert "failures" in loaded["result"]
        assert "uncertain" in loaded["result"]
        assert loaded["result"]["failures"][0]["category"] == "deadlock"
        store.close()
