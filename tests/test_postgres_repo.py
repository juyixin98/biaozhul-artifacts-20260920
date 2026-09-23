"""PostgreSQL 仓储集成测试。

默认跳过, 设置 LIQREPLAY_TEST_DATABASE_URL 后运行:
    LIQREPLAY_TEST_DATABASE_URL=postgresql://liqreplay:liqreplay@127.0.0.1:55460/liqreplay \
        .venv/bin/pytest tests/test_postgres_repo.py
"""
from __future__ import annotations

import os
import uuid

import pytest

from app.crypto import generate_private_key, public_hex, sign_payload
from app.repository import PostgresRepo

DB_URL = os.environ.get("LIQREPLAY_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB_URL, reason="no test database configured")

WAD = 10**18
T = 1_700_000_000


@pytest.fixture
def repo():
    r = PostgresRepo(DB_URL)
    r.init_schema()
    yield r


def _signed(event_id, ts=0, seq=0):
    key = generate_private_key()
    body = {
        "event_id": event_id, "ts": ts, "seq": seq,
        "action": "RATE_SCHEDULE",
        "payload": {"effective_ts": ts, "annual_rate_wad": 0},
    }
    return {**body, "signer": public_hex(key), "signature": sign_payload(key, body)}


def test_ping(repo):
    assert repo.ping() is True


def test_insert_idempotent_and_dedup(repo):
    suffix = uuid.uuid4().hex[:12]
    eid = f"evt-{suffix}"
    ev = _signed(eid, ts=T)
    assert repo.insert_events([ev]) == 1
    assert repo.insert_events([ev]) == 0  # ON CONFLICT DO NOTHING
    assert eid in repo.get_existing_ids([eid])
    stored = next(e for e in repo.all_engine_events() if e["event_id"] == eid)
    assert stored["payload"]["annual_rate_wad"] == 0
    assert stored["ts"] == T


def test_report_append_only_versions(repo):
    from app.engine import build_report

    ev = _signed(f"evt-rep-{uuid.uuid4().hex[:8]}")
    repo.insert_events([ev])
    body = build_report(repo.all_engine_events())
    v1 = repo.save_report(body, "sig-1", "INITIAL")

    repo.insert_events([_signed(f"evt-x-{uuid.uuid4().hex[:8]}", ts=T + 1)])
    body2 = build_report(repo.all_engine_events())
    v2 = repo.save_report(body2, "sig-2", "NEW_EVENTS")
    assert v2 == v1 + 1
    # 旧版本原样可读
    assert repo.get_report(v1)["hash"] != repo.get_report(v2)["hash"]
    versions = [x["version"] for x in repo.list_reports(limit=5)]
    assert versions[0] >= v2
    assert repo.get_latest_version() >= v2
