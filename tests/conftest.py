"""pytest 共享夹具：用仓库内已签名馈送构建 TestClient。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from sbom_risk.app import create_app, Settings  # noqa: E402
from sbom_risk.feed import load_feed  # noqa: E402


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return _REPO_ROOT


@pytest.fixture(scope="session")
def loaded_feed(repo_root):
    return load_feed(
        repo_root / "fixtures" / "vuln-feed.json",
        repo_root / "fixtures" / "keys" / "test_feed_public.pem",
    )


@pytest.fixture(scope="session")
def client(repo_root):
    settings = Settings()
    app = create_app(settings)
    with TestClient(app) as c:
        yield c
