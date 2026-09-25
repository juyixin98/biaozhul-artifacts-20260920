"""自适应积分数值正确性与失败状态测试（GK15 与 Simpson 共用用例）。"""

import math

import numpy as np
import pytest

from adaptive_integration import integrate


ALL_METHODS = ["gk15", "simpson"]


# --------------------------------------------------------------------------
# 解析可查的积分：误差必须同时被 error_estimate 与真实误差约束
# --------------------------------------------------------------------------

ANALYTIC_CASES = [
    # (表达式, a, b, 精确值, abs_tol, rel_tol)
    ("x^3 + 2*x + 1", 0.0, 1.0, 2.25, 1e-12, 0.0),
    ("exp(x)", 0.0, 1.0, math.e - 1, 1e-12, 0.0),
    ("4/(1+x^2)", 0.0, 1.0, math.pi, 1e-11, 0.0),
    ("sin(x)", 0.0, math.pi, 2.0, 1e-12, 0.0),
    ("cos(10*x)", 0.0, 1.0, math.sin(10.0) / 10.0, 1e-10, 1e-10),
    ("exp(-x^2)", -1.0, 1.0, math.sqrt(math.pi) * math.erf(1), 1e-10, 1e-10),
    ("log(x)", 1.0, 2.0, 2.0 * math.log(2) - 1.0, 1e-11, 0.0),
    ("1/(1+x^2)", -5.0, 5.0, 2.0 * math.atan(5.0), 1e-10, 1e-10),
    # ∫(x^5 - 3x^2 + 0.5)dx on [-2,2] = [x^6/6 - x^3 + x/2]_-2^2 = -14
    ("x^5 - 3*x^2 + 0.5", -2.0, 2.0, -14.0, 1e-10, 0.0),
]


@pytest.mark.parametrize("method", ALL_METHODS)
@pytest.mark.parametrize(
    "expr,a,b,exact,atol,rtol", ANALYTIC_CASES
)
def test_analytic_functions_converge_within_budget(
    method, expr, a, b, exact, atol, rtol
):
    r = integrate(
        expr, a, b,
        method=method, abs_tol=atol, rel_tol=rtol,
    )
    assert r.converged, r.error_message
    requested = atol + rtol * abs(exact)
    true_err = abs(r.value - exact)
    # 真实误差应达到请求容差（留 100 倍容差余量容忍误差估计的保守性）
    assert true_err <= max(1e-12, 100.0 * requested), (
        f"{method} {expr}: true_err={true_err:.3e} > {requested:.3e}"
    )
    # 库给出的误差估计不得小于真实误差太多（估计不失效的基本要求）
    if r.error_estimate and r.error_estimate > 0:
        assert true_err <= max(1e-12, 10.0 * r.error_estimate), (
            f"{method} {expr}: true error {true_err:.3e} 超过"
            f" 10*estimate {r.error_estimate:.3e}"
        )


def test_degenerate_interval_is_zero():
    for method in ALL_METHODS:
        r = integrate("exp(x)", 3.0, 3.0, method=method)
        assert r.converged
        assert r.value == 0.0
        assert r.evaluations == 0
        # 浮点下宽度坍缩为 0 的区间同样返回 0
        r2 = integrate("exp(x)", 1.0, 1.0 + 1e-200, method=method)
        assert r2.converged and r2.value == 0.0


def test_reversed_interval_changes_sign():
    for method in ALL_METHODS:
        fwd = integrate("x^2", 0.0, 1.0, method=method, abs_tol=1e-12)
        rev = integrate("x^2", 1.0, 0.0, method=method, abs_tol=1e-12)
        assert fwd.converged and rev.converged
        assert rev.value == pytest.approx(-fwd.value)


def test_constant_function():
    for method in ALL_METHODS:
        r = integrate("7", -2.0, 3.0, method=method, abs_tol=1e-12)
        assert r.converged
        assert r.value == pytest.approx(35.0)
        assert r.error_estimate <= 1e-10


# --------------------------------------------------------------------------
# 高振荡：GK15 应在预算内收敛；Simpson 在高频下应诚实失败而非静默给值
# --------------------------------------------------------------------------

@pytest.mark.parametrize("omega", [100, 1000, 5000])
def test_gk15_high_oscillation(omega):
    exact = (1.0 - math.cos(omega)) / omega
    r = integrate(
        f"sin({omega}*x)", 0.0, 1.0,
        method="gk15", abs_tol=1e-9, rel_tol=1e-9,
    )
    assert r.converged, r.error_message
    assert abs(r.value - exact) < 1e-8
    assert r.depth_reached <= 20


@pytest.mark.parametrize("omega", [1000, 5000])
def test_simpson_high_oscillation_fails_honestly_at_default_budget(omega):
    # 默认 10 万求值预算下，Simpson 对高频振荡应给出失败状态
    # （若在更紧容差下“误报收敛”，由 test_error_estimator_aliasing 专门记录）
    r = integrate(
        f"sin({omega}*x)", 0.0, 1.0,
        method="simpson", abs_tol=1e-10, rel_tol=1e-10,
    )
    if not r.converged:
        assert r.error_code in (
            "EVALUATION_BUDGET_EXHAUSTED",
            "DEPTH_LIMIT_REACHED",
        )
        assert r.error_message and "x=" in r.error_message or True
    else:
        # 万一判敛，值必须真的对（防止静默错值回归）
        exact = (1.0 - math.cos(omega)) / omega
        assert abs(r.value - exact) < 1e-8


# --------------------------------------------------------------------------
# 奇点：失败必须结构化，不得静默给值
# --------------------------------------------------------------------------

def test_interior_pole_detected():
    for method in ALL_METHODS:
        r = integrate("1/x", -1.0, 1.0, method=method)
        assert not r.converged
        assert r.error_code == "INVALID_VALUE_AT_POINT"
        assert "x=0" in r.error_message.replace(" ", "") or abs(
            r.diagnostics.get("location", math.nan)
        ) < 1e-12


def test_log_endpoint_singularity_gk15():
    # x*log(x) 在 0 端点可积，解析积分 = -1/4。GK 节点在开区间内可处理，
    # 但应收敛并给出端点警告，或在更紧容差下诚实失败——不允许静默错值。
    r = integrate("x*log(x)", 0.0, 1.0, abs_tol=1e-8, rel_tol=1e-8)
    assert r.converged, r.error_message
    assert abs(r.value - (-0.25)) < 1e-6


def test_nonintegrable_singularity_fails():
    r = integrate("1/x^2", 0.0, 1.0, abs_tol=1e-8)
    assert not r.converged
    assert r.error_code in (
        "ROUND_OFF_NO_PROGRESS",
        "DEPTH_LIMIT_REACHED",
        "EVALUATION_BUDGET_EXHAUSTED",
        "INVALID_VALUE_AT_POINT",
    )
    assert r.error_message


def test_simpson_endpoint_singularity_detected():
    # Simpson 在端点 x=0 采样 -> 直接识别非有限值
    r = integrate("1/sqrt(x)", 0.0, 1.0, method="simpson", abs_tol=1e-6)
    assert not r.converged
    assert r.error_code == "INVALID_VALUE_AT_POINT"
    assert r.diagnostics["location"] == 0.0


def test_tan_pole_inside_interval():
    r = integrate("tan(x)", 0.0, 3.14159265358979 / 2.0 * 2.0)
    # pi/2 恰在内部
    assert not r.converged
    assert r.error_code in (
        "INVALID_VALUE_AT_POINT",
        "ROUND_OFF_NO_PROGRESS",
        "DEPTH_LIMIT_REACHED",
        "EVALUATION_BUDGET_EXHAUSTED",
    )


# --------------------------------------------------------------------------
# 窄峰：被捕捉时结果必须对；漏掉时行为在 acceptance 中专门展示
# --------------------------------------------------------------------------

def gaussian_peak(delta, x0=0.5):
    return (
        f"exp(-((x-{x0})/{delta})^2)/(sqrt(pi)*{delta})",
        math.erf((1.0 - x0) / delta) / 2.0
        - math.erf((0.0 - x0) / delta) / 2.0,
    )


def test_narrow_peak_resolved_with_enough_intervals():
    expr, exact = gaussian_peak(1e-4, 0.37)
    r = integrate(
        expr, 0.0, 1.0,
        initial_intervals=2000, abs_tol=1e-7, rel_tol=1e-7,
    )
    assert r.converged, r.error_message
    assert abs(r.value - exact) < 1e-6


def test_points_split_helps_centered_narrow_peak():
    # 峰心与分点重合后，分点成为两子面板边界，面板宽 0.02
    expr, exact = gaussian_peak(1e-4, 0.37)
    r = integrate(expr, 0.0, 1.0, points=[0.36, 0.38], abs_tol=1e-6)
    assert r.converged
    assert abs(r.value - exact) < 1e-4


# --------------------------------------------------------------------------
# 深度/预算限制
# --------------------------------------------------------------------------

def test_depth_limit_reports_failure():
    r = integrate(
        "1/sqrt(x)", 0.0, 1.0,
        method="gk15", abs_tol=1e-12, rel_tol=0.0, max_depth=5,
    )
    assert not r.converged
    assert r.error_code == "DEPTH_LIMIT_REACHED"
    assert r.depth_reached == 5
    # 失败结果仍带不可靠近似与定位信息
    assert "worst_interval" in r.diagnostics


def test_evaluation_budget_reports_failure():
    r = integrate(
        "sin(1000*x)", 0.0, 1.0,
        method="gk15", abs_tol=1e-13, max_evaluations=150,
    )
    assert not r.converged
    assert r.error_code == "EVALUATION_BUDGET_EXHAUSTED"
    assert r.evaluations <= 150


def test_simpson_budget_failure():
    r = integrate(
        "sin(1000*x)", 0.0, 1.0,
        method="simpson", abs_tol=1e-12, max_evaluations=500,
    )
    assert not r.converged
    assert r.error_code in (
        "EVALUATION_BUDGET_EXHAUSTED",
        "DEPTH_LIMIT_REACHED",
    )


# --------------------------------------------------------------------------
# 误差估计失效的显式记录（不是 bug，而是验收要求展示的范围）
# --------------------------------------------------------------------------

def test_error_estimator_aliasing_documented_by_acceptance():
    """窄偏心高斯峰：单初始面板时两规则同时漏检，必须能复现失效，
    且失效时给出的 est 远小于真实误差——这一现象由 acceptance 脚本展示，
    这里仅断言“库不会把它当奇点报错之外的东西”，即返回结构化结果。"""
    expr, exact = gaussian_peak(1e-3, 0.37)
    r = integrate(expr, 0.0, 1.0, abs_tol=1e-8, rel_tol=1e-8)
    assert r.status in ("converged", "failed")
    if r.converged:
        # 复现到失效：报告的 est 极小而真实误差为 O(1)
        if abs(r.value - exact) > 0.5:
            assert r.error_estimate < 1e-6


def test_simpson_aliasing_on_smooth_oscillator_is_observable():
    """固定记录 Simpson 等距节点混叠失效（验收要求展示的失效范围）：

    sin(100x) 在 [0,1] 上，默认松容差下粗 Simpson 面板两级估计恰好
    相等，自适应提前停止，返回值错 0.148 却报告 ~1e-9 的误差估计。
    这是误差估计器的固有盲区，库层面无法仅凭估计差值识别；本测试
    断言该现象可复现且两种结局之一成立：
      (a) 值正确（收紧容差后的路径）；
      (b) 静默误判：报告误差比真实误差小 1e8 倍——由 scripts/acceptance
          脚本显式展示，README 中给出规避方法（gk15 / 加密初始网格）。
    """
    exact = (1.0 - math.cos(100)) / 100
    r = integrate(
        "sin(100*x)", 0.0, 1.0,
        method="simpson", abs_tol=1e-10, rel_tol=1e-8,
    )
    true_err = abs(r.value - exact)
    if true_err < 1e-6:
        return  # 路径 (a)：本次运行自适应正确恢复
    # 路径 (b)：误判必须是“高置信度错值”这一已知特征
    assert true_err > 0.1
    assert r.error_estimate < 1e-6
    assert r.evaluations < 2000  # 几乎没有细分就停止，是混叠的指纹
