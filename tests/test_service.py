"""服务编排测试: 成功路径与失败分类。"""

from __future__ import annotations

import pytest

from eigenservice.config import EigenConfig
from eigenservice.errors import (
    AsymmetryError,
    NonConvergenceError,
    QualityCheckError,
)
from eigenservice.service import decompose
from eigenservice.trace import RequestLog

from .fixtures import mildly_asymmetric_matrix, random_symmetric, with_spectrum


@pytest.mark.unit
def test_service_success_carries_evidence_and_trace() -> None:
    matrix = with_spectrum([-2.0, 0.5, 3.0], seed=7)
    log = RequestLog(request_id="req-success-1")
    result = decompose(matrix.tolist(), log=log)

    assert result.size == 3
    assert result.eigenvalues == pytest.approx([-2.0, 0.5, 3.0], abs=1e-10)
    assert result.quality["residual"]["relative_fro"] < 1e-10
    assert result.quality["orthogonality"]["deviation_fro"] < 1e-10
    assert result.quality["reconstruction"]["relative_fro"] < 1e-10
    # 可解释性: 请求身份、版本、关键步骤齐全
    trace = result.trace
    assert trace["request_id"] == "req-success-1"
    assert trace["service_version"]
    assert trace["core"] == "householder-tridiagonalization+implicit-shift-qrl"
    step_names = [s["step"] for s in trace["steps"]]
    assert "input_validated" in step_names
    assert "kernel_finished" in step_names
    assert "quality_gate_passed" in step_names
    assert trace["failures"] == []


@pytest.mark.unit
def test_service_asymmetry_classified() -> None:
    matrix = mildly_asymmetric_matrix(1e-5)
    with pytest.raises(AsymmetryError) as exc:
        decompose(matrix, EigenConfig(sym_tol=1e-9))
    assert exc.value.code == "asymmetric_matrix"
    assert exc.value.details["relative_asymmetry"] > 1e-9


@pytest.mark.unit
def test_service_zero_budget_is_failure_not_success() -> None:
    # 稠密 6x6 矩阵, 迭代预算为 0: 停止迭代绝不等于成功
    matrix = random_symmetric(6, seed=21)
    config = EigenConfig(base_sweeps=0, sweep_multiplier=0)
    with pytest.raises(NonConvergenceError) as exc:
        decompose(matrix, config)

    assert exc.value.code == "not_converged"
    assert exc.value.details["max_sweeps"] == 0
    assert exc.value.details["stalled_index"] is not None
    # 失败原因与不确定结论单列
    trace = exc.value.details["trace"]
    assert trace["failures"][0]["code"] == "not_converged"
    assert len(trace["uncertainties"]) == 1
    assert "partial_eigenvalues" in trace["uncertainties"][0]["detail"]


@pytest.mark.unit
def test_service_tight_quality_threshold_classified() -> None:
    # 内核结果正确 (残差 ~1e-16), 但把阈值压到不可能达到 -> quality_check_failed
    matrix = random_symmetric(5, seed=33)
    config = EigenConfig(residual_tol=0.0)
    with pytest.raises(QualityCheckError) as exc:
        decompose(matrix, config)
    assert exc.value.code == "quality_check_failed"
    assert "relative_residual" in exc.value.details["violations"]
    trace = exc.value.details["trace"]
    assert trace["failures"][0]["code"] == "quality_check_failed"
    assert trace["uncertainties"]


@pytest.mark.unit
def test_service_budget_scales_with_size_config() -> None:
    matrix = random_symmetric(4, seed=44)
    # 充足预算成功
    ok = decompose(matrix, EigenConfig(base_sweeps=10, sweep_multiplier=12))
    assert ok.sweeps >= 1
    # 预算公式随规模变化
    assert EigenConfig(base_sweeps=10, sweep_multiplier=12).max_sweeps_for(100) == 1200
    assert EigenConfig(base_sweeps=50, sweep_multiplier=1).max_sweeps_for(3) == 50


@pytest.mark.unit
def test_quality_gate_classifies_each_violation_kind() -> None:
    from eigenservice.evidence import (
        OrthogonalityEvidence,
        ReconstructionEvidence,
        ResidualEvidence,
    )
    from eigenservice.service import _quality_violations

    good_res = ResidualEvidence(0.0, 0.0, (0.0,), 0.0)
    bad_res = ResidualEvidence(1.0, 1.0, (1.0,), 1.0)
    good_ort = OrthogonalityEvidence(0.0, 0.0, 0.0)
    bad_ort = OrthogonalityEvidence(1.0, 1.0, 1.0)
    good_rec = ReconstructionEvidence(0.0, 0.0)
    bad_rec = ReconstructionEvidence(1.0, 1.0)

    assert _quality_violations(EigenConfig(), good_res, good_ort, good_rec) == {}
    violations = _quality_violations(
        EigenConfig(), bad_res, bad_ort, bad_rec
    )
    assert set(violations) == {
        "relative_residual",
        "orthogonality_deviation",
        "relative_reconstruction",
    }
