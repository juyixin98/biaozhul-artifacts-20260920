"""Shared pytest helpers: fixture loading and payload parsing."""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = REPO_ROOT / "data" / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    with (FIXTURE_DIR / name).open(encoding="utf-8") as handle:
        return json.load(handle)


def to_fraction(value: Any) -> Fraction:
    """Parse test-side scalars; floats are forbidden here too."""
    if isinstance(value, Fraction):
        return value
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, str):
        return Fraction(value)
    raise TypeError(f"test data must be exact, got {type(value)}")


def fixture_matrix(raw) -> list[list[Fraction]]:
    return [[to_fraction(v) for v in row] for row in raw]


def fixture_vector(raw) -> list[Fraction]:
    return [to_fraction(v) for v in raw]


def payload_fraction(item: dict[str, Any]) -> Fraction:
    return Fraction(item["num"], item["den"])


@pytest.fixture
def log_dir(tmp_path, monkeypatch):
    """Point the JSONL run log at an isolated temp directory."""
    path = tmp_path / "logs"
    monkeypatch.setenv("RATIONAL_LINALG_LOG_DIR", str(path))
    return path
