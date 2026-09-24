"""Shared pytest fixtures."""

import pytest

from app.models import AnalysisRequest


@pytest.fixture
def schedulable_payload():
    return {
        "tasks": [
            {"id": "T1", "wcet": 1, "period": 4, "deadline": 3, "blocking": 0, "priority": 1},
            {"id": "T2", "wcet": 2, "period": 8, "deadline": 8, "blocking": 1, "priority": 2},
            {"id": "T3", "wcet": 3, "period": 12, "deadline": 12, "blocking": 0, "priority": 3},
        ]
    }


@pytest.fixture
def low_u_unschedulable_payload():
    # U = 1/3 + 2/7 = 0.619, yet T2 (D=2) misses its deadline.
    return {
        "tasks": [
            {"id": "T1", "wcet": 1, "period": 3, "deadline": 3, "blocking": 0, "priority": 1},
            {"id": "T2", "wcet": 2, "period": 7, "deadline": 2, "blocking": 0, "priority": 2},
        ]
    }


@pytest.fixture
def blocking_miss_payload():
    return {
        "tasks": [
            {"id": "T1", "wcet": 2, "period": 10, "deadline": 10, "blocking": 0, "priority": 1},
            {"id": "T2", "wcet": 2, "period": 5, "deadline": 5, "blocking": 3, "priority": 2},
        ]
    }


@pytest.fixture
def analyze():
    from app.analyzer import analyze as _analyze

    def _run(payload):
        return _analyze(AnalysisRequest.model_validate(payload))

    return _run
