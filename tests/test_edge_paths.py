"""边界路径测试：构造器校验、无轮次/已终结状态、CLI 空分片等。

集中覆盖"正常场景走不到"的防御分支，避免错误信息路径未经测试。
"""

from __future__ import annotations

import numpy as np
import pytest

from gradbucket import worker_cli
from gradbucket.diagnostics import Verdict
from gradbucket.graph import LinearModel, ModelConfig
from gradbucket.tensors import ParamSpec, build_layout, pack_bucket
from gradbucket.training import RoundCoordinator, sgd_step
from gradbucket.runtime import FakeClock
from gradbucket.diagnostics import DiagnosticLog
from gradbucket.reference import union_batch_gradient


def fresh_coord():
    return RoundCoordinator(liveness_timeout=5.0, clock=FakeClock(),
                            diaglog=DiagnosticLog())


def begin(coord, **kw):
    model = LinearModel(ModelConfig())
    params = kw.pop("params", model.param_specs())
    initial = {p.name: p.zeros() for p in params}
    workers = kw.pop("expected_workers", ["w0", "w1", "w2"])
    return coord.begin_round(
        0, params, initial, workers,
        bucket_capacity=2, known_zero_params=["spare"], **kw
    )


# --- ParamSpec / 布局 -------------------------------------------------------


def test_paramspec_rejects_empty_name_bad_shape_and_dtype():
    with pytest.raises(ValueError, match="不能为空"):
        ParamSpec("", (2,))
    with pytest.raises(ValueError, match="形状非法"):
        ParamSpec("x", (0, 3))
    with pytest.raises(ValueError, match="float64"):
        ParamSpec("x", (2,), dtype="float32")
    # 别名归一
    assert ParamSpec("x", (2,), dtype="double").dtype == "float64"
    assert ParamSpec("x", (2,)).zeros().shape == (2,)


def test_build_layout_rejects_nonpositive_capacity():
    with pytest.raises(ValueError, match="bucket_capacity"):
        build_layout(0, [ParamSpec("a", (1,))], 0)


def test_layout_slot_by_name_and_empty_layout():
    layout = build_layout(0, [ParamSpec("a", (1,)), ParamSpec("b", (2,))], 2)
    assert layout.slot_by_name("b").index == 1
    with pytest.raises(KeyError):
        layout.slot_by_name("missing")
    with pytest.raises(ValueError, match="至少"):
        build_layout(0, [], 2)


def test_pack_allow_missing_fills_zeros_for_absent_slot():
    layout = build_layout(0, [ParamSpec("a", (2,)), ParamSpec("b", (1,))], 2)
    vec = pack_bucket(layout, 0, {"a": np.ones(2)}, allow_missing=True)
    np.testing.assert_array_equal(vec, [1.0, 1.0, 0.0])


# --- training 状态机防御分支 ------------------------------------------------


def test_sgd_step_requires_positive_lr():
    w = {"a": np.zeros(2)}
    with pytest.raises(ValueError, match="learning_rate"):
        sgd_step(w, {"a": np.ones(2)}, 0.0)
    # 不修改入参
    out = sgd_step(w, {"a": np.ones(2)}, 0.5)
    np.testing.assert_array_equal(w["a"], np.zeros(2))
    np.testing.assert_array_equal(out["a"], [-0.5, -0.5])


def test_coordinator_starts_without_round():
    coord = fresh_coord()
    assert coord.current_round_index() is None
    assert coord.round_view() is None
    with pytest.raises(RuntimeError, match="没有轮次"):
        coord.heartbeat("w0")
    with pytest.raises(RuntimeError, match="没有轮次"):
        coord.seal_round()
    coord.reset_rejected_round()  # 无轮次时为安全 no-op


def test_submit_without_round_is_rejected():
    coord = fresh_coord()
    v = coord.submit_bucket(0, 0, "w0", 0, np.zeros(1),
                            np.ones(1, dtype=bool), 2)
    assert v == Verdict.REJECTED_STALE_ROUND


def test_begin_validates_workers_weights_and_overlapping_open_round():
    coord = fresh_coord()
    with pytest.raises(ValueError, match="重复"):
        begin(coord, expected_workers=["w0", "w0"])
    with pytest.raises(ValueError, match="至少"):
        begin(coord, expected_workers=[])
    model = LinearModel(ModelConfig())
    params = model.param_specs()
    with pytest.raises(ValueError, match="缺少参数"):
        coord.begin_round(0, params, {"w": np.zeros((2, 3))}, ["w0"],
                          bucket_capacity=2)
    begin(coord)
    with pytest.raises(RuntimeError, match="仍开放"):
        begin(coord)


def test_submit_bucket_index_out_of_range_rejected():
    coord = fresh_coord()
    begin(coord)
    coord.heartbeat("w0")
    v = coord.submit_bucket(0, 0, "w0", 7, np.zeros(8),
                            np.ones(8, dtype=bool), 2)
    assert v == Verdict.REJECTED_BUCKET_SHAPE


def test_heartbeat_unknown_worker_raises():
    coord = fresh_coord()
    begin(coord)
    with pytest.raises(KeyError):
        coord.heartbeat("ghost")


def test_reset_open_round_rejected_but_reset_after_reject_works():
    coord = fresh_coord()
    begin(coord)
    with pytest.raises(RuntimeError, match="开放轮"):
        coord.reset_rejected_round()
    # 制造一个被拒轮（全员从未心跳）。
    coord._clock.advance(9.0)
    verdict, _ = coord.seal_round()
    assert verdict == Verdict.REJECTED_WORKER_LOST
    coord.reset_rejected_round()
    assert coord.round_view() is None
    # reset 后可以开新一轮。
    begin(coord)


# --- reference 边界 ---------------------------------------------------------


def test_union_reference_rejects_empty_shard_set():
    model = LinearModel(ModelConfig())
    with pytest.raises(ValueError, match="没有任何分片"):
        union_batch_gradient(model, [])
    with pytest.raises(ValueError, match="空分片"):
        union_batch_gradient(
            model, [(np.zeros((0, 3)), np.zeros((0, 2)))]
        )


# --- worker_cli 边界 --------------------------------------------------------


class TestClient:
    """记录调用的极简 HTTP stub，供 worker_cli 离线测试。"""


def test_worker_cli_empty_shard_returns_code_2(monkeypatch):
    # rank=0 独占全部但 n_samples=0 的退化场景在 shard_indices 之前不会触发；
    # 直接让分片切片为空：n_workers=2 但某 rank 得到 0 样本需要 shard_sizes。
    # graph.shard_indices 不允许零大小，因此这里通过 monkeypatch 模拟。
    import gradbucket.worker_cli as wc
    import gradbucket.graph as graph_mod

    class FakeResp:
        status_code = 200

        def json(self):
            return {}

        def raise_for_status(self):
            pass

    class FakeHttpClient:
        def __init__(self, *a, **k):
            pass

        def get(self, path):
            if path == "/rounds/current":
                return _Json({
                    "round_index": 0,
                    "bucket_sizes": [2, 1],
                    "slots": [
                        {"param": "w", "offset": 0, "length": 6, "shape": [2, 3]},
                        {"param": "b", "offset": 6, "length": 2, "shape": [2]},
                        {"param": "spare", "offset": 8, "length": 2, "shape": [2]},
                    ],
                })
            return _Json({"generation": 0,
                          "weights": {"w": [[0] * 3] * 2, "b": [0, 0]}})

        def post(self, path, json=None):
            return FakeResp()

    class _Json(FakeResp):
        def __init__(self, payload):
            self._p = payload

        def json(self):
            return self._p

    monkeypatch.setattr(wc.httpx, "Client", FakeHttpClient)
    # 让 shard_indices 返回空索引（绕过其自身校验，专门驱动空分片分支）。
    monkeypatch.setattr(graph_mod, "shard_indices",
                        lambda *a, **k: np.array([], dtype=int))
    rc = wc.run([
        "--base-url", "http://x", "--worker-id", "w0", "--rank", "0",
        "--n-workers", "1", "--n-samples", "0", "--linger-seconds", "0",
    ])
    assert rc == 2
