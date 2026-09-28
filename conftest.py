"""Pytest fixtures shared by all tests.

Lives at the repository root so the root directory is on ``sys.path`` and
``import app`` works from a clean checkout.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture()
def settings(tmp_path):
    return Settings(db_path=str(tmp_path / "lexer.db"))


@pytest.fixture()
def client(settings):
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def small_input_client(tmp_path):
    """A client whose input-length limit is tiny, for RESOURCE_EXHAUSTED tests."""
    app = create_app(
        Settings(db_path=str(tmp_path / "lexer.db"), max_input_chars=8)
    )
    with TestClient(app) as test_client:
        yield test_client


def post_ruleset(client, name, rules):
    return client.post("/api/rulesets", json={"name": name, "rules": rules})


def create_ok(client, name, rules):
    response = post_ruleset(client, name, rules)
    assert response.status_code == 201, response.json()
    return response.json()
