"""Shared pytest fixtures: isolated data dir, generated synthetic boards,
FastAPI test client."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "examples" / "fixtures"


def _ensure_fixtures() -> Path:
    marker = FIXTURES / "ground_truth.json"
    valid = FIXTURES / "valid_views"
    if not marker.exists() or len(list(valid.glob("*.png"))) != 24:
        subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "generate_fixtures.py"), str(FIXTURES)],
            check=True,
            capture_output=True,
        )
    return FIXTURES


@pytest.fixture(scope="session", autouse=True)
def fixtures_ready() -> Path:
    return _ensure_fixtures()


@pytest.fixture()
def fx(fixtures_ready):
    return {
        "root": fixtures_ready,
        "gt": json.loads((fixtures_ready / "ground_truth.json").read_text()),
    }


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    d = (tmp_path / "data").resolve()
    d.mkdir()
    monkeypatch.setenv("CALIB_DATA_DIR", str(d))
    monkeypatch.setenv("CALIB_HMAC_KEY", "test-secret-key-for-hmac-only")

    # Drop every cached app.* module so app.config re-reads the env vars and
    # every dependent module rebinds to the new values.
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]

    import app.config as config

    assert config.DATA_DIR == d
    monkeypatch.setattr(config, "IMAGE_ROOTS", [ROOT.resolve(), d])
    yield d


@pytest.fixture()
def client(data_dir):
    from app.main import app  # fresh import bound to the patched config

    with TestClient(app) as c:
        yield c


def read_images(subdir: str) -> list[tuple[str, bytes]]:
    d = FIXTURES / subdir
    return [(p.name, p.read_bytes()) for p in sorted(d.glob("*.png"))]
