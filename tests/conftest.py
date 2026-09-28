"""Shared pytest fixtures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from strips_planner.evidence import EvidenceStore
from strips_planner.grounding import ground
from strips_planner.parser import parse_domain, parse_problem
from strips_planner.validation import validate

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def resource_domain():
    return load("domain_resource_ops.json")


@pytest.fixture
def route_domain():
    return load("domain_route_graph.json")


@pytest.fixture
def grounded(resource_domain):
    def _make(problem_file):
        problem = load(problem_file)
        domain = parse_domain(resource_domain)
        prob = parse_problem(problem, domain)
        validate(domain, prob)
        return ground(domain, prob)

    return _make


@pytest.fixture
def store(tmp_path):
    db = EvidenceStore(str(tmp_path / "evidence.db"))
    yield db
    db.close()


@pytest.fixture
def plan_request():
    def _make(domain_file, problem_file, options=None):
        req = {
            "domain": load(domain_file),
            "problem": load(problem_file),
        }
        if options is not None:
            req["options"] = options
        return req

    return _make
