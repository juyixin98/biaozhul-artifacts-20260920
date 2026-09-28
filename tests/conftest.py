"""Shared fixtures: high-precision reference data and matching helpers."""

from __future__ import annotations

import json
import os

import pytest

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "reference_roots.json")


@pytest.fixture(scope="session")
def reference() -> dict:
    with open(FIXTURE_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def ref_roots(case: dict) -> list[complex]:
    return [complex(float(r["re"]), float(r["im"])) for r in case["reference_roots"]]


def as_pairs(coeffs) -> list[list[float]]:
    """Integer/real coefficient list -> wire format [[re, im], ...]."""
    return [[float(c), 0.0] for c in coeffs]


def match_max_error(computed: list[complex], expected: list[complex]) -> float:
    """Greedy one-to-one matching; returns the worst |computed - expected|."""
    remaining = list(expected)
    worst = 0.0
    for z in computed:
        best = min(remaining, key=lambda w: abs(z - w))
        worst = max(worst, abs(z - best))
        remaining.remove(best)
    return worst
