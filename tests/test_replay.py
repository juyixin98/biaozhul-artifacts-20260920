"""乱序/有序一致性与检查点重放测试（本项目核心验收点）。"""
from __future__ import annotations

import json
import os

import numpy as np

from app.fusion import FusionEngine

DATA = os.path.join(os.path.dirname(os.path.dirname(__file__)), "examples", "data")
RNG = np.random.default_rng(4242)


def _load(name):
    with open(os.path.join(DATA, name), encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _run(engine: FusionEngine, msgs):
    for m in msgs:
        engine.ingest(
            mid=m["id"],
            mtype=m["type"],
            t=m["time"],
            measurement=m["measurement"],
            R=m["R"],
            seq=m.get("seq", 0),
        )
    return engine


def _assert_same_traces(a: FusionEngine, b: FusionEngine):
    ta = {s["id"]: s for s in a.trace_view(limit=10000)}
    tb = {s["id"]: s for s in b.trace_view(limit=10000)}
    assert set(ta) == set(tb), "两边处理的测量集合不同"
    for mid in ta:
        ea, eb = ta[mid], tb[mid]
        assert ea["accepted"] == eb["accepted"], f"{mid} 接受决策不同"
        assert np.allclose(ea["innovation"], eb["innovation"], atol=1e-12), (
            f"{mid} 创新量不同"
        )
        assert np.allclose(ea["nis"], eb["nis"], rtol=1e-12, atol=1e-12), (
            f"{mid} NIS 不同: {ea['nis']} vs {eb['nis']}"
        )
        assert np.allclose(ea["posterior"]["x"], eb["posterior"]["x"], atol=1e-11), (
            f"{mid} 后验状态不同"
        )
        assert np.allclose(ea["P"], eb["P"], atol=1e-11), f"{mid} 协方差不同"
    sa, sb = a.state_view()["state"], b.state_view()["state"]
    assert np.allclose(sa["x"], sb["x"], atol=1e-11)
    assert np.allclose(sa["P"], sb["P"], atol=1e-11)


def test_shuffled_example_matches_ordered_example():
    ordered = _load("ordered.jsonl")
    shuffled = _load("shuffled.jsonl")
    assert [m["id"] for m in shuffled][:3] != [m["id"] for m in ordered][:3]

    e_ordered = _run(FusionEngine(), ordered)
    e_shuffled = _run(FusionEngine(), shuffled)
    _assert_same_traces(e_ordered, e_shuffled)


def test_random_shuffles_all_agree():
    """多组“随机到达延迟”重排：保证任意时刻到达的消息测量时间都在 2s 窗口内，
    结果必须与严格有序引擎逐位一致。"""
    msgs = _load("ordered.jsonl")
    subset = sorted(
        (m for m in msgs if m["time"] <= 5.0),
        key=lambda m: (m["time"], 0 if m["type"] == "odometry" else 1, m["id"]),
    )

    ref = _run(FusionEngine(), subset)
    for trial in range(6):
        rng = np.random.default_rng(100 + trial)
        # 首条必须最先到达（建立初始时间），其余消息按 time + lag 排序，
        # lag ∈ [0.1, 1.9) ⇒ 最早测量与“当前最新测量”之差始终 < 2s
        first, rest = subset[0], subset[1:]
        arrivals = [
            (m["time"] + float(rng.uniform(0.1, 1.9)), m["id"], m) for m in rest
        ]
        arrivals.sort(key=lambda x: (x[0], x[1]))
        order = [first] + [m for _, _, m in arrivals]
        e = _run(FusionEngine(), order)
        _assert_same_traces(ref, e)


def test_late_message_replays_from_checkpoint_and_corrects_state():
    e = FusionEngine()
    e.ingest(mid="o1", mtype="odometry", t=0.0, measurement=[1.0, 0.0],
             R=[[0.01, 0], [0, 0.01]])
    r2 = e.ingest(mid="o2", mtype="odometry", t=0.2, measurement=[1.0, 0.0],
                  R=[[0.01, 0], [0, 0.01]])
    r3 = e.ingest(mid="g1", mtype="gnss", t=0.4, measurement=[0.0, 0.0],
                  R=[[0.5, 0], [0, 0.5]])
    # 0.4s 时没有迟到的 GNSS，x 位置应已被恒速预测推到 ~0.4
    x_without_late = r3["state"]["x"][:]

    # 现在来一条 0.1s 的迟到 GNSS（窗口内）：应从检查点重放，
    # 后续所有状态被重新计算，位置被显著拉回。
    r_late = e.ingest(mid="g-late", mtype="gnss", t=0.1, measurement=[0.0, 0.0],
                      R=[[0.2, 0], [0, 0.2]])
    assert r_late["status"] == "processed"
    assert r_late["replayed"] >= 2
    # 轨迹严格按测量时间排序，迟到消息插在 o1 与 o2 之间
    times = [(s["time"], s["id"]) for s in e.trace_view()]
    assert times == sorted(times)
    assert r_late["step"]["time"] == 0.1
    # 重放后 0.4s 的最终状态应与“没有迟到消息”不同（迟到 GNSS 真的改变了结果）
    assert not np.allclose(x_without_late, e.state_view()["state"]["x"], atol=1e-6)


def test_full_replay_equivalent_to_pristine_ordered_run():
    """构造乱序到达序列后，与同数据严格有序的独立引擎逐位一致。"""
    msgs = _load("ordered.jsonl")
    subset = [m for m in msgs if m["time"] <= 3.0]
    # 故意：GNSS 先到（较晚时间），里程计迟到（较早时间）
    gnss_first = [m for m in subset if m["type"] == "gnss"][:2]
    rest = [m for m in subset if m["id"] not in {m["id"] for m in gnss_first}]
    arrival = gnss_first + rest

    e_pristine = _run(FusionEngine(), subset)
    e_arrival = _run(FusionEngine(), arrival)
    _assert_same_traces(e_pristine, e_arrival)


def test_checkpoints_and_buffer_carry_state():
    e = FusionEngine(horizon_s=2.0)
    for i in range(40):
        e.ingest(mid=f"o{i}", mtype="odometry", t=i * 0.1, measurement=[1.0, 0.0],
                 R=[[0.01, 0], [0, 0.01]])
    view = e.state_view()
    # 2s 窗口内约 21 条测量 + 检查点
    assert 18 <= view["buffered"] <= 24
    assert view["checkpoints"] >= view["buffered"] - 2
