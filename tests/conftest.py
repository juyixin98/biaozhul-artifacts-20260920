"""Shared pytest fixtures and loaders."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from htn_planner.core.engine import Bounds
from htn_planner.lang import Domain, Problem, parse_domain, parse_problem

ROOT = Path(__file__).resolve().parent.parent
DOMAINS = ROOT / "fixtures" / "domains"
PROBLEMS = ROOT / "fixtures" / "problems"
EXPECTED = ROOT / "fixtures" / "expected"

# (domain_file, problem_file, expected_file)
FIXTURE_CASES = [
    ("logistics.htn", "logistics_direct.pddl", "logistics_direct.json"),
    ("logistics.htn", "logistics_recursive.pddl", "logistics_recursive.json"),
    ("logistics.htn", "logistics_ship.pddl", "logistics_ship.json"),
    ("logistics.htn", "logistics_unreachable.pddl", "logistics_unreachable.json"),
    ("docks.htn", "docks_two.pddl", "docks_two.json"),
    ("docks.htn", "docks_three.pddl", "docks_three.json"),
    ("permit_deadlock.htn", "permit_two.pddl", "permit_two.json"),
    ("bounds.htn", "bounds_spin.pddl", "bounds_spin.json"),
    ("bounds.htn", "bounds_walk_long.pddl", "bounds_walk_long.json"),
]


def load_domain(filename: str) -> Domain:
    return parse_domain((DOMAINS / filename).read_text())


def load_problem(filename: str) -> Problem:
    return parse_problem((PROBLEMS / filename).read_text())


def load_expected(filename: str) -> dict[str, Any]:
    return json.loads((EXPECTED / filename).read_text())


@pytest.fixture(scope="session")
def standard_bounds() -> Bounds:
    return Bounds(max_depth=12, max_expansions=500, max_actions=100)


@pytest.fixture
def logistics_domain() -> Domain:
    return load_domain("logistics.htn")


@pytest.fixture
def docks_domain() -> Domain:
    return load_domain("docks.htn")


@pytest.fixture
def permit_domain() -> Domain:
    return load_domain("permit_deadlock.htn")


@pytest.fixture
def bounds_domain() -> Domain:
    return load_domain("bounds.htn")
