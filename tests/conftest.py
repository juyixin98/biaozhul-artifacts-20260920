"""Shared pytest helpers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from strips_planner.language.parser import parse_dict
from strips_planner.language.validator import validate

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


def load_fixture(name: str) -> dict:
    with (FIXTURES_DIR / name).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def build_problem(payload: dict, *, max_ground_actions: int = 50_000):
    return validate(parse_dict(payload), max_ground_actions=max_ground_actions)


@pytest.fixture
def resource_payload() -> dict:
    return load_fixture("resource_ops.json")


@pytest.fixture
def resource_problem(resource_payload):
    return build_problem(resource_payload)


@pytest.fixture
def unsolvable_payload() -> dict:
    return load_fixture("resource_ops_unsolvable.json")


@pytest.fixture
def unsolvable_problem(unsolvable_payload):
    return build_problem(unsolvable_payload)
