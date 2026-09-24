"""快照存储测试：SHA-256/HMAC-SHA256 全部真实计算，篡改必须被检出。"""

from __future__ import annotations

import json

import pytest

from qos_diag.models import Endpoint, QoSValues, Topology
from qos_diag.rules import RULES_VERSION
from qos_diag.store import SnapshotStore, canonical_payload


def make_topology() -> Topology:
    ep = Endpoint(
        topic="/t",
        endpoint_type="publisher",
        node_name="n",
        endpoint_gid="g1",
        topic_type="std_msgs/msg/String",
        qos=QoSValues(),
        first_seen=1.0,
        last_seen=2.0,
    )
    return Topology(captured_at=3.0, discovery_interval=1.0, missing_grace_seconds=5.0,
                    endpoints=[ep], events=[])


def test_save_and_load_roundtrip(tmp_path):
    store = SnapshotStore(tmp_path)
    snap = store.create_snapshot(make_topology())
    path = store.save(snap)
    loaded = store.load(path.name)
    assert loaded.snapshot_id == snap.snapshot_id
    assert loaded.rules_version == RULES_VERSION
    assert len(loaded.diagnoses) == 1


def test_sha256_matches_independent_hashlib(tmp_path):
    import hashlib

    store = SnapshotStore(tmp_path)
    snap = store.create_snapshot(make_topology())
    store.save(snap)
    files = list(tmp_path.glob("snapshot-*.json"))
    envelope = json.loads(files[0].read_text())
    expect = hashlib.sha256(envelope["payload"].encode()).hexdigest()
    assert envelope["sha256"] == expect


def test_hmac_matches_independent_hmaclib(tmp_path):
    import hashlib
    import hmac

    store = SnapshotStore(tmp_path)
    snap = store.create_snapshot(make_topology())
    store.save(snap)
    files = list(tmp_path.glob("snapshot-*.json"))
    envelope = json.loads(files[0].read_text())
    key = (tmp_path / ".hmac_key").read_bytes().strip()
    expect = hmac.new(key, envelope["payload"].encode(), hashlib.sha256).hexdigest()
    assert envelope["hmac_sha256"] == expect
    # 密钥文件必须是受限权限
    assert oct((tmp_path / ".hmac_key").stat().st_mode & 0o777) == "0o600"


def test_payload_tampering_detected_via_sha(tmp_path):
    store = SnapshotStore(tmp_path)
    snap = store.create_snapshot(make_topology())
    path = store.save(snap)
    raw = json.loads(path.read_text())
    raw["payload"] = raw["payload"].replace("publisher", "publisherX", 1)
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="SHA-256"):
        store.load(path.name)


def test_hmac_forgery_detected(tmp_path):
    """攻击者即使重新计算 sha256，没有密钥也无法伪造 HMAC。"""
    import hashlib

    store = SnapshotStore(tmp_path)
    snap = store.create_snapshot(make_topology())
    path = store.save(snap)
    raw = json.loads(path.read_text())
    raw["payload"] = raw["payload"].replace("publisher", "publisherX", 1)
    raw["sha256"] = hashlib.sha256(raw["payload"].encode()).hexdigest()  # 伪造哈希
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="HMAC"):
        store.load(path.name)


def test_wrong_key_rejected(tmp_path):
    import hashlib
    import hmac

    store1 = SnapshotStore(tmp_path)
    snap = store1.create_snapshot(make_topology())
    path = store1.save(snap)

    other = tmp_path / "other"
    other.mkdir()
    store2 = SnapshotStore(other)
    # 用不同密钥重签一份 HMAC 覆盖掉原文件
    raw = json.loads(path.read_text())
    raw["hmac_sha256"] = hmac.new(
        (other / ".hmac_key").read_bytes().strip(),
        raw["payload"].encode(), hashlib.sha256
    ).hexdigest()
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="HMAC"):
        store1.load(path.name)


def test_canonical_payload_is_deterministic(tmp_path):
    store = SnapshotStore(tmp_path)
    snap = store.create_snapshot(make_topology())
    assert canonical_payload(snap) == canonical_payload(snap.model_copy(deep=True))


def test_env_key_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("QOSDIAG_HMAC_KEY", "unit-test-secret")
    store = SnapshotStore(tmp_path)
    assert store.key_id == "env:QOSDIAG_HMAC_KEY"
    snap = store.create_snapshot(make_topology())
    path = store.save(snap)
    assert store.load(path.name).snapshot_id == snap.snapshot_id
