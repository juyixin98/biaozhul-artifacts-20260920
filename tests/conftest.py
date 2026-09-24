"""Shared pytest fixtures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.signing import (
    TrustStore,
    generate_keypair,
    public_key_pem,
    sign_document,
)

REPO = Path(__file__).resolve().parents[1]
EXAMPLES = REPO / "examples"


@pytest.fixture(scope="session")
def keypair():
    return generate_keypair()


@pytest.fixture(scope="session")
def trust(keypair) -> TrustStore:
    store = TrustStore()
    store.add_pem(public_key_pem(keypair[1]))
    return store


@pytest.fixture
def signer(keypair):
    priv, _pub = keypair

    def _sign(document):
        return sign_document(document, priv)

    return _sign


@pytest.fixture
def client(trust) -> TestClient:
    # Tests cover both unsigned and signed paths: enable unsigned explicitly.
    app = create_app(trust, allow_unsigned=True)
    return TestClient(app)


@pytest.fixture
def strict_client(trust) -> TestClient:
    # Production-like: inline documents rejected.
    app = create_app(trust, allow_unsigned=False)
    return TestClient(app)


@pytest.fixture
def demo_policy() -> dict:
    return json.loads((EXAMPLES / "policy.json").read_text())


@pytest.fixture
def demo_policy_set() -> dict:
    return json.loads((EXAMPLES / "policy_set.json").read_text())
