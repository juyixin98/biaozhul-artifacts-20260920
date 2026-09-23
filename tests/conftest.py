"""Shared pytest fixtures."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from provenance_service.app import app  # noqa: E402
from provenance_service.fixtures import sample_job_parts  # noqa: E402,F401
from provenance_service.storage import Store  # noqa: E402

ADDR_A = "0x8ba1f109551bD432803012645Ac136ddd64DBA72"


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "t.db"), str(tmp_path / "k.pem"))


@pytest.fixture
def client(tmp_path):
    from fastapi.testclient import TestClient

    os.environ["PROVENANCE_DB"] = str(tmp_path / "api.db")
    os.environ["PROVENANCE_KEY"] = str(tmp_path / "api.pem")
    # Reload app state with the temp paths.
    app.state.store = Store(os.environ["PROVENANCE_DB"], os.environ["PROVENANCE_KEY"])
    return TestClient(app)


@pytest.fixture
def happy_parts():
    return sample_job_parts()
