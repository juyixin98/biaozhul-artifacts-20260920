"""持久化与重启重建：投票与检查点落库，重启后重放重建同一结论。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app

from .conftest import BLOCK_A, BLOCK_B, make_validators, post_vote, vote_payload


def _build_finalized_db(db_path: str) -> None:
    app = create_app(db_path)
    with TestClient(app) as c:
        validators, keys = make_validators([("a", 1), ("b", 1), ("c", 1)])
        r = c.post("/epochs", json={"epoch": 70, "validators": validators})
        assert r.status_code == 201
        for v in ("a", "b", "c"):
            post_vote(c, vote_payload(keys, 70, v, BLOCK_A))
        assert c.get("/epochs/70/status").json()["state"] == "finalized"


def test_restart_rebuilds_same_conclusion(tmp_path):
    db = str(tmp_path / "restart.db")
    _build_finalized_db(db)

    # 重新打开同一数据库文件：启动时 reconcile 重放并核对
    app2 = create_app(db)
    with TestClient(app2) as c2:
        assert c2.get("/health").json()["reconciled_epochs"] == [70]
        st = c2.get("/epochs/70/status").json()
        assert st["state"] == "finalized"
        assert st["finalized_block"] == BLOCK_A
        assert st["finalized_weight"] == 3
        cps = c2.get("/checkpoints").json()
        assert len(cps) == 1 and cps[0]["block_hash"] == BLOCK_A


def test_restart_rebuilds_frozen_conflict(tmp_path):
    db = str(tmp_path / "frozen.db")
    app = create_app(db)
    with TestClient(app) as c:
        validators, keys = make_validators([("a", 1), ("b", 1), ("c", 1), ("d", 1)])
        c.post("/epochs", json={"epoch": 71, "validators": validators})
        for v in ("a", "b", "c"):
            post_vote(c, vote_payload(keys, 71, v, BLOCK_A))
        post_vote(c, vote_payload(keys, 71, "a", BLOCK_B))
        post_vote(c, vote_payload(keys, 71, "b", BLOCK_B))
        post_vote(c, vote_payload(keys, 71, "d", BLOCK_B))
        assert c.get("/epochs/71/status").json()["state"] == "frozen_conflict"

    app2 = create_app(db)
    with TestClient(app2) as c2:
        st = c2.get("/epochs/71/status").json()
        assert st["state"] == "frozen_conflict"
        assert st["finalized_block"] == BLOCK_A  # 检查点未因重启/新消息改变
        assert st["conflict"]["conflicting_block"] == BLOCK_B
        # 冻结状态在重启后依然生效
        r = post_vote(c2, vote_payload(keys, 71, "c", BLOCK_B))
        assert r.status_code == 409


def test_restart_detects_tampered_checkpoint(tmp_path):
    import sqlite3

    db = str(tmp_path / "tampered.db")
    _build_finalized_db(db)

    # 直接篡改库中的 checkpoint，模拟状态损坏
    conn = sqlite3.connect(db)
    conn.execute("UPDATE checkpoints SET block_hash=? WHERE epoch=70", (BLOCK_B,))
    conn.commit()
    conn.close()

    try:
        create_app(db)
    except RuntimeError as e:
        assert "checkpoint 分歧" in str(e)
    else:
        raise AssertionError("篡改 checkpoint 后启动应当失败")
