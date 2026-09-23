"""测试夹具: 签名助手 + 内存仓储服务。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.crypto import generate_private_key, public_hex, sign_payload  # noqa: E402
from app.repository import MemoryRepo  # noqa: E402
from app.service import ReplayService  # noqa: E402


@pytest.fixture
def operator_key():
    return generate_private_key()


@pytest.fixture
def other_key():
    return generate_private_key()


@pytest.fixture
def server_key():
    return generate_private_key()


@pytest.fixture
def make_event(operator_key):
    """构造一条完整签名事件。默认 signer 为受信任操作者。"""

    def _make(
        event_id,
        ts,
        seq,
        action,
        payload,
        key=None,
        bad_signature=False,
    ):
        key = key or operator_key
        body = {
            "event_id": event_id,
            "ts": ts,
            "seq": seq,
            "action": action,
            "payload": payload,
        }
        sig = sign_payload(key, body)
        if bad_signature:
            sig = "00" * 64
        return {**body, "signer": public_hex(operator_key), "signature": sig}

    return _make


@pytest.fixture
def svc(operator_key, server_key):
    repo = MemoryRepo()
    trusted = {public_hex(operator_key): operator_key.public_key()}
    return ReplayService(repo, trusted, server_key)
