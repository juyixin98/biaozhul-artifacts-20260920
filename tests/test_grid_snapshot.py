"""快照链哈希绑定与变更原子性测试。"""

import numpy as np
import pytest

from app.grid import MapStore, SnapshotNotFound, StaleSnapshot, canonical_state_bytes


def _genesis(store: MapStore, seed=0):
    rng = np.random.RandomState(seed)
    w = rng.uniform(0.5, 3.0, (store.rows, store.cols))
    b = np.zeros((store.rows, store.cols), dtype=bool)
    return store.commit_genesis(w, b, "2026-09-23T00:00:00+00:00"), w, b


def test_genesis_snapshot_id_is_sha256_hex():
    s = MapStore(4, 4, 8)
    snap, _, _ = _genesis(s)
    assert len(snap.snapshot_id) == 64
    assert all(ch in "0123456789abcdef" for ch in snap.snapshot_id)
    assert snap.parent_id is None
    assert snap.version == 0


def test_snapshot_id_deterministic_for_same_state():
    s1 = MapStore(3, 3, 8)
    s2 = MapStore(3, 3, 8)
    a, _, _ = _genesis(s1, 7)
    b, _, _ = _genesis(s2, 7)
    assert a.snapshot_id == b.snapshot_id


def test_snapshot_id_changes_with_state():
    s1 = MapStore(3, 3, 8)
    s2 = MapStore(3, 3, 8)
    a, _, _ = _genesis(s1, 1)
    b, _, _ = _genesis(s2, 2)
    assert a.snapshot_id != b.snapshot_id


def test_update_chains_parent_and_advances_version():
    s = MapStore(5, 5, 8)
    g, _, _ = _genesis(s)
    nxt = s.apply_changes(g, [{"row": 1, "col": 1, "kind": "block"}], "t2")
    assert nxt.version == 1
    assert nxt.parent_id == g.snapshot_id
    assert nxt.snapshot_id != g.snapshot_id
    assert s.head is nxt


def test_update_must_build_on_head():
    s = MapStore(5, 5, 8)
    g, _, _ = _genesis(s)
    s.apply_changes(g, [{"row": 1, "col": 1, "kind": "block"}], "t2")
    with pytest.raises(StaleSnapshot):
        # 试图在旧的 genesis 上分叉提交
        s.apply_changes(g, [{"row": 2, "col": 2, "kind": "block"}], "t3")


def test_negative_weight_rejected_atomically():
    s = MapStore(3, 3, 8)
    g, _, _ = _genesis(s)
    with pytest.raises(ValueError):
        s.apply_changes(
            g,
            [
                {"row": 0, "col": 0, "kind": "weight", "weight": 2.0},
                {"row": 1, "col": 1, "kind": "weight", "weight": -1.0},
            ],
            "t2",
        )
    # 原子性: 链头仍为 genesis, 地图未被部分修改
    assert s.head is g
    assert not s.head.blocked[0, 0]
    assert len(s.snapshots) == 1


def test_nan_and_inf_weights_rejected():
    s = MapStore(2, 2, 8)
    g, _, _ = _genesis(s)
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            s.apply_changes(
                g,
                [{"row": 0, "col": 0, "kind": "weight", "weight": bad}],
                "t",
            )


def test_out_of_bounds_change_rejected():
    s = MapStore(3, 3, 8)
    g, _, _ = _genesis(s)
    with pytest.raises(ValueError):
        s.apply_changes(g, [{"row": 9, "col": 0, "kind": "block"}], "t2")


def test_block_free_weight_semantics():
    s = MapStore(2, 2, 8)
    snap, w, b = _genesis(s)
    w0 = float(snap.weights[0, 0])
    snap = s.apply_changes(
        snap,
        [
            {"row": 0, "col": 0, "kind": "weight", "weight": 7.5},
            {"row": 0, "col": 0, "kind": "block"},
        ],
        "t2",
    )
    assert snap.blocked[0, 0]
    assert snap.weights[0, 0] == 7.5  # 障碍期间保留权重
    snap = s.apply_changes(snap, [{"row": 0, "col": 0, "kind": "free"}], "t3")
    assert not snap.blocked[0, 0]
    assert snap.weights[0, 0] == 7.5  # 解除后权重还在
    assert w0 != 7.5


def test_verify_chain_reports_ok_on_clean_chain():
    s = MapStore(4, 4, 8)
    snap, _, _ = _genesis(s)
    for i in range(3):
        snap = s.apply_changes(
            snap, [{"row": i, "col": i, "kind": "block"}], f"t{i+2}"
        )
    report = s.verify_chain()
    assert report["ok"] is True
    assert len(report["snapshots"]) == 4
    assert all(x["ok"] for x in report["snapshots"])


def test_verify_detects_tampered_state():
    s = MapStore(4, 4, 8)
    snap, _, _ = _genesis(s)
    s.apply_changes(snap, [{"row": 0, "col": 0, "kind": "block"}], "t2")
    # 直接篡改快照内的地图状态(模拟存储被破坏)
    s.snapshots[1].weights[0, 0] = 999.0
    report = s.verify_chain()
    assert report["ok"] is False
    assert any(not x["ok"] for x in report["snapshots"])


def test_verify_detects_tampered_parent_pointer():
    s = MapStore(3, 3, 8)
    g, _, _ = _genesis(s)
    s.apply_changes(g, [{"row": 0, "col": 0, "kind": "block"}], "t2")
    object.__setattr__(s.snapshots[1], "parent_id", "deadbeef")
    assert s.verify_chain()["ok"] is False


def test_get_and_require_version():
    s = MapStore(3, 3, 8)
    g, _, _ = _genesis(s)
    nxt = s.apply_changes(g, [{"row": 0, "col": 0, "kind": "block"}], "t2")
    assert s.require_version(0) is g
    assert s.require_version(1) is nxt
    assert s.get(nxt.snapshot_id) is nxt
    with pytest.raises(SnapshotNotFound):
        s.require_version(99)
    assert s.get("nope") is None


def test_canonical_state_bytes_stable():
    w = np.array([[1.0, 2.0], [3.0, 4.0]])
    b = np.array([[False, True], [False, False]])
    x = canonical_state_bytes(w, b)
    y = canonical_state_bytes(w.copy(), b.copy())
    assert x == y
