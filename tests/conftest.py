"""pytest 公共夹具。"""

import pytest

from app.builder import Repository
from app.storage import TrustStore


@pytest.fixture
def repo():
    return Repository.create()


@pytest.fixture
def store(tmp_path):
    s = TrustStore(tmp_path / "data")
    s.ensure_dirs()
    return s
