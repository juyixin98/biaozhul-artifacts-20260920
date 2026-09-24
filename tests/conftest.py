import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.pki_fixtures import TestPKI


@pytest.fixture(scope="session")
def pki() -> TestPKI:
    return TestPKI()


@pytest.fixture(scope="session")
def client() -> TestClient:
    return TestClient(app)
