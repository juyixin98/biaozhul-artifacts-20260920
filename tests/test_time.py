"""时间语义测试：迟到窗口、时间回跳、长缺测、重复与同刻排序。"""
from __future__ import annotations

import numpy as np

from app.fusion import FusionEngine


def test_message_beyond_2s_horizon_rejected():
    e = FusionEngine(horizon_s=2.0)
    for i in range(31):
        e.ingest(mid=f"o{i}", mtype="odometry", t=i * 0.1, measurement=[1.0, 0.0],
                 R=[[0.01, 0], [0, 0.01]])
    # 最新 3.0s；0.9s 的迟到测量已出窗
    r = e.ingest(mid="late", mtype="gnss", t=0.9, measurement=[0.5, 0.0],
                 R=[[0.5, 0], [0, 0.5]])
    assert r["accepted"] is False
    assert r["reason"] == "LATE_OUT_OF_HORIZON"
    assert r["evidence"]["detail"]["late_by_s"] > 2.0

    # 窗口边界内（恰好 2.0s）应被接受并重放（存在锚点）
    r_edge = e.ingest(mid="edge", mtype="gnss", t=1.0, measurement=[0.5, 0.0],
                      R=[[0.5, 0], [0, 0.5]])
    # 1.0s 测量的锚点可能已被裁剪到窗口下沿，仍应成功处理
    assert r_edge["accepted"] is True


def test_clock_back_jump_reorders_via_replay():
    """时间“回跳”：先收到较晚消息，再收到较早消息。
    不是直接按到达顺序融合，而是重放；结果应与有序到达一致。"""
    e = FusionEngine(horizon_s=2.0)
    e.ingest(mid="a", mtype="odometry", t=1.0, measurement=[1.0, 0.0],
             R=[[0.01, 0], [0, 0.01]])
    e.ingest(mid="b", mtype="odometry", t=1.2, measurement=[1.0, 0.0],
             R=[[0.01, 0], [0, 0.01]])
    # 时间回跳到 1.1（窗口内）
    r = e.ingest(mid="c", mtype="gnss", t=1.1, measurement=[1.1, 0.0],
                 R=[[0.2, 0], [0, 0.2]])
    assert r["accepted"] is True
    assert r["replayed"] >= 1
    times = [s["time"] for s in e.trace_view()]
    assert times == sorted(times)

    ref = FusionEngine(horizon_s=2.0)
    ref.ingest(mid="a", mtype="odometry", t=1.0, measurement=[1.0, 0.0],
               R=[[0.01, 0], [0, 0.01]])
    ref.ingest(mid="c", mtype="gnss", t=1.1, measurement=[1.1, 0.0],
               R=[[0.2, 0], [0, 0.2]])
    ref.ingest(mid="b", mtype="odometry", t=1.2, measurement=[1.0, 0.0],
               R=[[0.01, 0], [0, 0.01]])
    assert np.allclose(
        e.state_view()["state"]["x"], ref.state_view()["state"]["x"], atol=1e-11
    )


def test_long_outage_propagates_and_recovers():
    e = FusionEngine()
    e.ingest(mid="o0", mtype="odometry", t=0.0, measurement=[1.0, 0.0],
             R=[[0.01, 0], [0, 0.01]])
    e.ingest(mid="g0", mtype="gnss", t=0.0, measurement=[0.0, 0.0],
             R=[[0.25, 0], [0, 0.25]])
    var_before = e.state_view()["state"]["P"][0][0]

    # 31 秒长缺测后的里程计
    r = e.ingest(mid="o-gap", mtype="odometry", t=31.0, measurement=[1.0, 0.0],
                 R=[[0.01, 0], [0, 0.01]])
    step = r["step"]
    P_pred = np.asarray(step["P_predicted"])
    # 更新前的预测协方差：长缺测使位置方差按 ~ q t^4/4 大幅膨胀
    assert P_pred[0, 0] > var_before * 50
    assert np.allclose(P_pred, P_pred.T)
    assert np.min(np.linalg.eigvalsh(P_pred)) > 0
    # 恒速预测把位置推进到约 31m
    assert abs(step["predicted"]["x"][0] - 31.0) < 0.01

    # GNSS 重新出现后，位置被拉回真值附近
    r2 = e.ingest(mid="g-recover", mtype="gnss", t=31.1, measurement=[31.0, 0.0],
                  R=[[0.25, 0], [0, 0.25]])
    assert r2["accepted"] is True
    assert r2["state"]["P"][0][0] < P_pred[0, 0]
    assert abs(r2["state"]["x"][0] - 31.0) < 1.0


def test_duplicate_id_is_idempotent():
    e = FusionEngine()
    r1 = e.ingest(mid="dup", mtype="gnss", t=0.0, measurement=[0.0, 0.0],
                  R=[[0.5, 0], [0, 0.5]])
    r2 = e.ingest(mid="dup", mtype="gnss", t=0.0, measurement=[999.0, 999.0],
                  R=[[0.5, 0], [0, 0.5]])
    assert r2["status"] == "duplicate"
    assert e.state_view()["buffered"] == 1
    assert r1["state"]["x"] == r2["state"]["x"]


def test_same_time_messages_follow_deterministic_order():
    e = FusionEngine(horizon_s=2.0)
    e.ingest(mid="x", mtype="gnss", t=1.0, seq=5, measurement=[0.0, 0.0],
             R=[[0.5, 0], [0, 0.5]])
    # 同时间、更早 seq 的迟到消息：odometry 排在同 seq gnss 之前
    r = e.ingest(mid="v", mtype="odometry", t=1.0, seq=1, measurement=[1.0, 0.0],
                 R=[[0.01, 0], [0, 0.01]])
    assert r["accepted"] is True
    ids = [(s["seq"], s["type"], s["id"]) for s in e.trace_view()]
    assert ids == sorted(ids, key=lambda x: (x[0], 0 if x[1] == "odometry" else 1))
