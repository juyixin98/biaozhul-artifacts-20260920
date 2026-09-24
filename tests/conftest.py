"""Shared pytest helpers."""

from __future__ import annotations

import json
from pathlib import Path

from app.models import AnalyzeRequest

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def parse_fixture(name: str) -> AnalyzeRequest:
    return AnalyzeRequest.model_validate(load_fixture(name)["request"])
