"""Shared pytest configuration: make the flat package importable."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest

from certverifier.fixtures import build_all_scenarios


@pytest.fixture(scope="session")
def scenarios():
    """Generate every local test chain once per test session."""
    return build_all_scenarios()


@pytest.fixture(scope="session")
def verify():
    from certverifier.verify import verify_chain
    return verify_chain
