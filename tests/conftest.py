"""pytest 公共夹具。"""

from __future__ import annotations

import pytest

from keyversion.service import KeyService
from keyversion.store import KeyStore


@pytest.fixture
def store(tmp_path):
    s = KeyStore(tmp_path / "keystore").open()
    yield s
    s.close()


@pytest.fixture
def service(store):
    return KeyService(store)


@pytest.fixture
def initialized_service(service):
    """带一个 active 版本的服务。"""
    service.rotate()
    return service
