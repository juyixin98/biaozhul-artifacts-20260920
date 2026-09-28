"""Shared pytest fixtures."""
from __future__ import annotations

import json
import os

import pytest

DATA_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "fixtures.json")


@pytest.fixture(scope="session")
def fixtures() -> dict:
    with open(DATA_PATH, encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture()
def case(fixtures) -> dict:
    """Return the named compute case dict, e.g. case('big_common_factor')."""
    def _get(name: str) -> dict:
        return fixtures["cases"][name]
    return _get


@pytest.fixture()
def input_error_case(fixtures) -> dict:
    def _get(name: str) -> dict:
        return fixtures["input_error_cases"][name]
    return _get


@pytest.fixture()
def tmp_run_log(tmp_path, monkeypatch):
    """Point the API at a throwaway JSONL run log for the duration of a test."""
    path = tmp_path / "run_log.jsonl"
    monkeypatch.setenv("RATIONALSVC_RUN_LOG", str(path))
    # Re-import the module-level constant used by api.py.
    from rationalsvc import api
    monkeypatch.setattr(api, "RUN_LOG_PATH", str(path))
    return path
