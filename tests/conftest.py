"""Pytest configuration: make the repo root importable and point the app
at an isolated temporary SQLite database."""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SBOM_DB_PATH", str(ROOT / "data" / "test.sbom.db"))
os.environ.setdefault("SBOM_FIXTURE_PATH", str(ROOT / "data" / "vulnerabilities.json"))


@pytest.fixture(scope="session")
def client():
    from fastapi.testclient import TestClient

    db = Path(os.environ["SBOM_DB_PATH"])
    if db.exists():
        db.unlink()
    from app.main import app

    with TestClient(app) as c:
        yield c

    if db.exists():
        db.unlink()
