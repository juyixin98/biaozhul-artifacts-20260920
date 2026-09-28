"""Shared pytest configuration: isolated log path + project root on path."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hvpsvc.runs import RunLogger  # noqa: E402
from hvpsvc.service import HVPService  # noqa: E402
from hvpsvc.state import StateStore  # noqa: E402


@pytest.fixture
def log_path(tmp_path):
    return tmp_path / "runs.jsonl"


@pytest.fixture
def service(log_path):
    return HVPService(store=StateStore(), logger=RunLogger(log_path))
