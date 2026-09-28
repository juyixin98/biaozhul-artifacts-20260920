"""Shared pytest fixtures: offline-calibrated registry and TestClient.

Fixtures are built once per session from *synthetic* data; no network.
"""

from __future__ import annotations

import os
import sys

import numpy as np
import pytest

# Make the repo root importable when pytest is invoked from any cwd.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fastapi.testclient import TestClient  # noqa: E402

from app.bootstrap import build_registry  # noqa: E402
from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from scripts.generate_fixtures import generate  # noqa: E402


@pytest.fixture(scope="session")
def settings(tmp_path_factory) -> Settings:
    fixture_dir = str(tmp_path_factory.mktemp("fixtures"))
    generate(fixture_dir, "tiny-matmul-demo", "2026-09-28-v1")
    return Settings(
        model_id="tiny-matmul-demo",
        model_version="2026-09-28-v1",
        fixture_dir=fixture_dir,
    )


@pytest.fixture(scope="session")
def registry(settings):
    return build_registry(settings)


@pytest.fixture(scope="session")
def registered(registry, settings):
    return registry.get(settings.model_id)


@pytest.fixture(scope="session")
def client(settings) -> TestClient:
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def fixed_batch() -> np.ndarray:
    """Fixed batch shared with scripts/demo.py (3 hand-inspectable rows)."""
    return np.array(
        [
            [0.50, -1.00, 0.25, 0.75],
            [-0.50, 1.00, -0.25, -0.75],
            [0.00, 0.00, 0.00, 0.00],
        ],
        dtype=np.float32,
    )
