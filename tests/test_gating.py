"""门限拒绝（离群测量）与协方差合法性测试。"""
from __future__ import annotations

import numpy as np

from app.fusion import FusionEngine, validate_covariance


def test_outlier_rejected_with_evidence_but_state_advances():
    e = FusionEngine(gate_nis=9.2103)
    e.ingest(mid="g0", mtype="gnss", t=0.0, measurement=[0.0, 0.0],
             R=[[0.5, 0], [0, 0.5]])
    for i in range(1, 6):
        e.ingest(mid=f"o{i}", mtype="odometry", t=i * 0.1,
                 measurement=[1.0, 0.0], R=[[0.01, 0], [0, 0.01]])

    # 预期位置约 0.5m；放到 100m 处的 GNSS 是离群值
    r = e.ingest(mid="g-bad", mtype="gnss", t=0.5, measurement=[100.0, 100.0],
                 R=[[0.25, 0], [0, 0.25]])
    assert r["accepted"] is False
    assert r["reason"] == "OUTLIER_GATE"
    step = r["step"]
    assert step["nis"] > 9.2103
    assert step["gated"] is True
    # 证据齐全：创新量、S、NIS
    assert len(step["innovation"]) == 2
    assert np.asarray(step["S"]).shape == (2, 2)
    assert step["posterior"]["x"][0] < 5.0  # 状态没有被离群值带跑

    # 拒绝证据可查询
    rejections = e.rejections_view()
    assert any(x["reason"] == "OUTLIER_GATE" and x["id"] == "g-bad" for x in rejections)


def test_inlier_accepted_near_prediction():
    e = FusionEngine()
    e.ingest(mid="o0", mtype="odometry", t=0.0, measurement=[1.0, 0.0],
             R=[[0.01, 0], [0, 0.01]])
    r = e.ingest(mid="g0", mtype="gnss", t=0.1, measurement=[0.12, 0.0],
                 R=[[0.5, 0], [0, 0.5]])
    assert r["accepted"] is True
    assert r["step"]["nis"] <= 9.2103


def test_per_message_gate_override():
    # 同一条偏离预测的 GNSS：默认门限接受，逐条收紧到 0.2 后拒绝
    def prime_and_try(**kwargs):
        e = FusionEngine(gate_nis=9.2103)
        e.ingest(mid="g0", mtype="gnss", t=0.0, measurement=[0.0, 0.0],
                 R=[[0.5, 0], [0, 0.5]])
        e.ingest(mid="o1", mtype="odometry", t=0.1, measurement=[1.0, 0.0],
                 R=[[0.01, 0], [0, 0.01]])
        return e.ingest(mid="g1", mtype="gnss", t=0.2, measurement=[0.8, 0.0],
                        R=[[0.5, 0], [0, 0.5]], **kwargs)

    r_loose = prime_and_try()
    assert r_loose["accepted"] is True
    assert r_loose["step"]["nis"] > 0.2
    r_strict = prime_and_try(gate_nis=0.2)
    assert r_strict["accepted"] is False
    assert r_strict["step"]["gate_nis"] == 0.2
    assert r_strict["step"]["nis"] > 0.2


def test_validate_covariance_accepts_and_rejects():
    assert validate_covariance(np.array([[1.0, 0.2], [0.2, 1.0]]))[0]
    assert validate_covariance(np.diag([0.0, 1.0]))[0]  # 奇异但半正定

    bad_shape = validate_covariance(np.eye(3))
    assert bad_shape[0] is False and bad_shape[1]["problem"] == "shape"

    asym = validate_covariance(np.array([[1.0, 0.5], [0.2, 1.0]]))
    assert asym[0] is False and asym[1]["problem"] == "asymmetric"

    npsd = validate_covariance(np.array([[1.0, 0.9], [0.9, -1.0]]))
    assert npsd[0] is False and npsd[1]["problem"] == "not_psd"
    assert npsd[1]["min_eigenvalue"] < 0

    nan_cov = validate_covariance(np.array([[np.nan, 0.0], [0.0, 1.0]]))
    assert nan_cov[0] is False and nan_cov[1]["problem"] == "non_finite"


def test_bad_covariance_rejected_at_engine_and_not_stored():
    e = FusionEngine()
    r = e.ingest(mid="bad", mtype="gnss", t=0.0, measurement=[0.0, 0.0],
                 R=[[1.0, 0.9], [0.9, -1.0]])
    assert r["accepted"] is False
    assert r["reason"] == "BAD_COVARIANCE"
    assert e.state_view()["initialized"] is False
    assert e.state_view()["buffered"] == 0
