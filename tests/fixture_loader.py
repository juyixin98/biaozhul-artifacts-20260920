"""Helpers for loading bundled fixtures in tests."""
from __future__ import annotations

from pathlib import Path

from htn_planner import Planner, load_domain, load_problem
from htn_planner.models import PlanResult, Problem
from htn_planner.rule_language import RuleLanguageError

REPO_ROOT = Path(__file__).resolve().parents[1]
DOMAIN_DIR = REPO_ROOT / "fixtures" / "domains"
PROBLEM_DIR = REPO_ROOT / "fixtures" / "problems"


def plan_fixture(problem_file: str) -> tuple[PlanResult, object, Problem]:
    problem = load_problem(PROBLEM_DIR / problem_file)
    domain = load_domain(DOMAIN_DIR / f"{problem.domain}.yaml")
    return Planner(domain).plan(problem), domain, problem


def domain_path(name: str) -> Path:
    return DOMAIN_DIR / f"{name}.yaml"


def load_raw_problem(problem_file: str) -> dict:
    import yaml

    with open(PROBLEM_DIR / problem_file, encoding="utf-8") as fh:
        return yaml.safe_load(fh)
