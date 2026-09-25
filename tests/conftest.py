"""共享测试夹具与辅助。"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from mde.crypto import generate_signing_key
from mde.policy import InMemoryPolicyStore
from mde.service import ExportService


@pytest.fixture
def store():
    return InMemoryPolicyStore()


@pytest.fixture
def signing_key():
    return generate_signing_key()


@pytest.fixture
def service(store, signing_key):
    return ExportService(store, signing_private_key=signing_key)
