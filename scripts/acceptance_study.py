"""验收研究：可解析函数的真实误差对比 + 高振荡 / 窄峰 / 端点奇异。

所有请求都通过 JSON 接口 :func:`handle_request` 发出（同时检验字符串解析器），
真实误差由解析解精确计算。脚本不抛异常退出：失败（不收敛/奇点）本身就是
要记录的结果。

运行::

    python -m scripts.acceptance_study          # 写 results/ 并打印 Markdown
    python -m scripts.acceptance_study --stdout  # 只打印不写文件
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adaptive_integration import handle_request  # noqa: E402

RESULTS_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "results")


def call(expr, a, b, eps_abs=1e-8, eps_rel=1e-8, method="simpson",
         max_depth=20, max_evals=100_000):
    t0 = time.perf_counter()
    r = handle_request({
        "expression": expr, "a": a, "b": b,
        "eps_abs": eps_abs, "eps_rel": eps_rel,
        "method": method, "max_depth": max_depth, "max_evals": max_evals,
    })
    r.setdefault("n_evals", 0)
    r.setdefault("n_intervals", 0)
    r.setdefault("max_depth_reached", 0)
    r.setdefault("details", {})
    r["_wall_ms"] = (time.perf_counter() - t0) * 1000.0
    return r


# ---------------------------------------------------------------------------
# 第 1 部分：有解析解的光滑函数，做容差扫描，对比真实误差与误差估计
# ---------------------------------------------------------------------------

SMOOTH_CASES = [
    # (名称, 表达式, 积分下界, 上界, 精确值计算)
    ("多项式 x^3", "x**3", 0.0, 1.0, lambda: 0.25),
    ("exp(x)", "exp(x)", 0.0, 1.0, lambda: math.e - 1.0),
    ("exp(-x^2)", "exp(-x**2)", 0.0, 1.0,
     lambda: 0.5 * math.sqrt(math.pi) * math.erf(1.0)),
    ("sin(x)", "sin(x)", 0.0, math.pi, lambda: 2.0),
    ("1/(1+x^2)", "1/(1+x**2)", -1.0, 1.0, lambda: math.pi / 2.0),
]
TOLERANCES = [1e-4, 1e-6, 1e-8, 1e-10, 1e-12]


def study_tolerance_sweep():
    rows = []
    for name, expr, a, b, exact_fn in SMOOTH_CASES:
        exact = exact_fn()
        for tol in TOLERANCES:
            for method in ("simpson", "gauss"):
                r = call(expr, a, b, eps_abs=tol, eps_rel=tol, method=method)
                if r["converged"]:
                    true_err = abs(r["value"] - exact)
                    est = r["error_estimate"]
                    rows.append({
                        "case": name, "method": method, "tol": tol,
                        "converged": True, "value": r["value"],
                        "true_error": true_err, "error_estimate": est,
                        "est_over_true": (est / true_err) if true_err > 0 else None,
                        "n_evals": r["n_evals"],
                        "n_intervals": r["n_intervals"],
                        "max_depth": r["max_depth_reached"],
                        "wall_ms": round(r["_wall_ms"], 3),
                    })
                else:
                    rows.append({
                        "case": name, "method": method, "tol": tol,
                        "converged": False,
                        "error_code": r["error_code"],
                        "message": r["message"], "n_evals": r["n_evals"],
                    })
    return rows


# ---------------------------------------------------------------------------
# 第 2 部分：高振荡
#   2a 非整数倍频 k+0.25：精确积分非零，检验“收敛到对的值”或“诚实失败”
#   2b 整数倍频 k：精确积分为 0，采样点全部落在零点 -> 偶然正确但估计过小
# ---------------------------------------------------------------------------

OSCILLATION_CASES = [
    ("10.25 周期", 10.25),
    ("100.25 周期", 100.25),
    ("1000.25 周期", 1000.25),
    ("5000.25 周期", 5000.25),
    ("10000.25 周期", 10000.25),
]
ALIGNED_CASES = [10, 100, 1000, 5000, 10000]


def _exact_sine_integral(k):
    # ∫₀¹ sin(2π k x) dx = (1 - cos(2πk))/(2πk)
    return (1.0 - math.cos(2.0 * math.pi * k)) / (2.0 * math.pi * k)


def study_oscillations():
    rows = []
    for name, k in OSCILLATION_CASES:
        exact = _exact_sine_integral(k)
        expr = f"sin(2*pi*{k!r}*x)"
        for method in ("simpson", "gauss"):
            r = call(expr, 0.0, 1.0, method=method)
            row = {"case": name, "frequency_cycles": k, "method": method,
                   "aligned": False,
                   "converged": r["converged"],
                   "status": r["status"], "n_evals": r["n_evals"],
                   "n_intervals": r["n_intervals"],
                   "max_depth": r["max_depth_reached"],
                   "wall_ms": round(r["_wall_ms"], 3),
                   "exact": exact}
            if r["converged"]:
                row.update(value=r["value"], true_error=abs(r["value"] - exact),
                           error_estimate=r["error_estimate"])
            else:
                row.update(error_code=r["error_code"],
                           message=r["message"])
            rows.append(row)

    # 整数周期：真实积分恰为 0；记录“答案对、估计过度乐观”这一现象
    for k in ALIGNED_CASES:
        expr = f"sin(2*pi*{k!r}*x)"
        for method in ("simpson", "gauss"):
            r = call(expr, 0.0, 1.0, method=method)
            row = {"case": f"整数周期 {k}", "frequency_cycles": k,
                   "method": method, "aligned": True,
                   "converged": r["converged"],
                   "status": r["status"], "n_evals": r["n_evals"],
                   "n_intervals": r["n_intervals"],
                   "max_depth": r["max_depth_reached"],
                   "wall_ms": round(r["_wall_ms"], 3),
                   "exact": 0.0}
            if r["converged"]:
                row.update(value=r["value"], true_error=abs(r["value"]),
                           error_estimate=r["error_estimate"])
            else:
                row.update(error_code=r["error_code"], message=r["message"])
            rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# 第 3 部分：误差估计失效
#   3a Simpson 采样混叠（sin 与 Simpson 网格同相）
#   3b 端点处窄峰（初始/外推网格绕过峰体）
# ---------------------------------------------------------------------------

def study_estimator_failure():
    rows = []

    # 3a 混叠：sin(16πx+0.3) 在 Simpson 所有层级采样点同相
    alias_expr = "sin(16*pi*x+0.3)"
    for method in ("simpson", "gauss"):
        r = call(alias_expr, 0.0, 1.0, 1e-10, 1e-10, method=method)
        row = {"case": "Simpson 混叠 sin(16πx+0.3)", "method": method,
               "converged": r["converged"], "n_evals": r["n_evals"],
               "error_estimate": r["error_estimate"],
               "true_error": (abs(r["value"] - 0.0)
                              if r["converged"] else None),
               "value": r["value"], "exact": 0.0}
        if not r["converged"]:
            row.update(error_code=r["error_code"], message=r["message"])
        rows.append(row)

    # 3b 端点窄峰：eps=1e-8 的 Lorentz 峰压在 x=0
    for eps in (1e-4, 1e-6, 1e-8):
        exact = 0.5 + math.atan(1.0 / eps) / math.pi
        expr = f"{eps!r}/(pi*(x**2+{eps ** 2!r}))"
        r_s = call(expr, 0.0, 1.0, 1e-8, 1e-8,
                   method="simpson", max_depth=40, max_evals=1_000_000)
        r_g = call(expr, 0.0, 1.0, 1e-8, 1e-8,
                   method="gauss", max_depth=40, max_evals=1_000_000)
        for label, r in (("simpson", r_s), ("gauss", r_g)):
            row = {"case": f"端点窄峰 eps={eps:g}", "method": label,
                   "converged": r["converged"], "n_evals": r["n_evals"],
                   "exact": exact,
                   "max_depth": r["max_depth_reached"]}
            if r["converged"]:
                row.update(value=r["value"],
                           true_error=abs(r["value"] - exact),
                           error_estimate=r["error_estimate"])
            else:
                row.update(error_code=r["error_code"], message=r["message"])
            rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# 第 4 部分：端点奇异 / 内部极点：必须失败说明，不静默给值；
#            再给出正则化（变量代换）后的成功对照
# ---------------------------------------------------------------------------

def study_singularities():
    rows = []

    singular = [
        ("端点弱奇异 x^{-1/2}", "1/sqrt(x)", 0.0, 1.0, 2.0,
         # x=t^2 代换：∫ 2 exp(0t) 型，这里演示纯幂函数 x^-1/2 -> 2
         "2", 0.0, 1.0),
        ("端点弱奇异 x^{-1/2}e^{-x}", "exp(-x)/sqrt(x)", 0.0, 1.0,
         math.sqrt(math.pi) * math.erf(1.0),
         "2*exp(-x**2)", 0.0, 1.0),
        ("端点对数奇异 sqrt(-ln x)", "sqrt(-log(x))", 0.0, 1.0,
         math.sqrt(math.pi) / 2.0,
         # 正确代换 x=e^{-u^2}：-ln x=u^2，dx=-2u e^{-u^2}du，
         # 积分变为 ∫_0^∞ 2 u^2 e^{-u^2} du = sqrt(pi)/2
         "2*x**2*exp(-x**2)", 0.0, 8.0),
    ]
    for name, expr, a, b, exact, rexpr, ra, rb in singular:
        r = call(expr, a, b)
        rows.append({"case": name, "kind": "直接积分",
                     "converged": r["converged"],
                     "error_code": r.get("error_code"),
                     "value": r.get("value"),
                     "n_evals": r["n_evals"],
                     "position": r.get("details", {}).get("position"),
                     "exact": exact,
                     "message": r["message"][:120]})
        rr = call(rexpr, ra, rb, 1e-10, 1e-10)
        rows.append({"case": name + "（变量代换后）", "kind": "正则化",
                     "converged": rr["converged"],
                     "error_code": rr.get("error_code"),
                     "value": rr.get("value"),
                     "true_error": (abs(rr["value"] - exact)
                                    if rr["converged"] else None),
                     "error_estimate": rr.get("error_estimate"),
                     "n_evals": rr["n_evals"], "exact": exact})

    # 内部极点：1/(x-0.5)
    r = call("1/(x-0.5)", 0.0, 1.0)
    rows.append({"case": "内部极点 1/(x-0.5)", "kind": "直接积分",
                 "converged": r["converged"],
                 "error_code": r.get("error_code"),
                 "position": r.get("details", {}).get("position"),
                 "value": r.get("value"), "n_evals": r["n_evals"],
                 "exact": None})
    # 可去奇点 sin(x)/x：字符串接口不支持条件表达式（白名单不含 IfExp），
    # 正则化版本直接用 Python 可调用对象演示（库的直接 API 入口）
    from adaptive_integration import integrate
    r1 = call("sin(x)/x", 0.0, 1.0)
    rows.append({"case": "可去奇点 sin(x)/x（未处理）", "kind": "直接积分",
                 "converged": r1["converged"],
                 "error_code": r1.get("error_code"),
                 "position": r1.get("details", {}).get("position"),
                 "value": r1.get("value"), "n_evals": r1["n_evals"],
                 "exact": None})
    r2q = integrate(lambda x: math.sin(x) / x if x != 0 else 1.0,
                    0.0, 1.0, 1e-10, 1e-10)
    rows.append({"case": "sin(x)/x（显式补极限 1，Python 函数入口）",
                 "kind": "正则化",
                 "converged": r2q.converged,
                 "value": r2q.value,
                 "true_error": (abs(r2q.value - 0.9460830703671830)
                                if r2q.converged else None),
                 "error_estimate": r2q.error_estimate,
                 "n_evals": r2q.n_evals, "exact": 0.9460830703671830})
    return rows


# ---------------------------------------------------------------------------
# 第 5 部分：窄中心峰——默认深度失败，放宽深度后成功（误差预算行为）
# ---------------------------------------------------------------------------

def study_narrow_peaks():
    rows = []
    for eps in (1e-2, 1e-4, 1e-6, 1e-8):
        expr = f"{eps!r}/(pi*((x-0.5)**2+{eps ** 2!r}))"
        exact = 2.0 * math.atan(0.5 / eps) / math.pi
        for depth in (20, 40):
            r = call(expr, 0.0, 1.0, 1e-8, 1e-8,
                     method="simpson", max_depth=depth, max_evals=1_000_000)
            row = {"case": f"中心窄峰 eps={eps:g}", "max_depth_allowed": depth,
                   "converged": r["converged"], "n_evals": r["n_evals"],
                   "n_intervals": r["n_intervals"],
                   "max_depth": r["max_depth_reached"], "exact": exact}
            if r["converged"]:
                row.update(value=r["value"],
                           true_error=abs(r["value"] - exact),
                           error_estimate=r["error_estimate"])
            else:
                row.update(error_code=r["error_code"],
                           message=r["message"][:120])
            rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Markdown 报告
# ---------------------------------------------------------------------------

def fmt(v, spec=".3e"):
    if v is None:
        return "—"
    if isinstance(v, float):
        return format(v, spec)
    return str(v)


def render_markdown(data) -> str:
    lines = []
    p = lines.append
    p("# 自适应积分验收结果\n")
    p(f"- Python {sys.version.split()[0]}，NumPy {__import__('numpy').__version__}")
    p("- 默认容差 1e-8（绝对+相对），默认深度 20，求值预算 100 000")
    p("- 全部请求经 JSON 接口（字符串表达式经安全 AST 解析器）\n")

    p("## 1. 光滑函数容差扫描：真实误差 vs 库内误差估计\n")
    p("精确值已知；`估计/真实` 比值应 >=1（估计偏保守）；真实误差应低于请求容差。\n")
    p("| 函数 | 方法 | 请求容差 | 真实误差 | 库内误差估计 | 估计/真实 | 求值数 | 最大深度 |")
    p("|---|---|---:|---:|---:|---:|---:|---:|")
    for r in data["sweep"]:
        if r["converged"]:
            ratio = fmt(r.get("est_over_true"), ".2f") \
                if r.get("est_over_true") is not None else "真实误差=0"
            p(f"| {r['case']} | {r['method']} | {r['tol']:.0e} "
              f"| {fmt(r['true_error'])} | {fmt(r['error_estimate'])} "
              f"| {ratio} | {r['n_evals']} | {r['max_depth']} |")
        else:
            p(f"| {r['case']} | {r['method']} | {r['tol']:.0e} "
              f"| **未收敛 {r['error_code']}** | — | — | {r['n_evals']} | — |")

    p("\n## 2. 高振荡\n")
    p("**2a 非整数倍频 k+0.25**（真实积分 = (1−cos(2πk))/(2πk) ≠ 0，"
      "采样不可能偶然命中）：频率升高时求积成本迅速上升，超出预算即诚实失败。\n")
    p("| 场景 | 方法 | 状态 | 返回值 | 精确值 | 真实误差 | 库内误差估计 | 求值数 | 深度 | 失败码 |")
    p("|---|---|---|---:|---:|---:|---:|---:|---:|---|")
    for r in data["oscillations"]:
        if r["aligned"]:
            continue
        if r["converged"]:
            p(f"| {r['case']} | {r['method']} "
              f"| 收敛 | {fmt(r['value'], '.8f')} "
              f"| {fmt(r['exact'], '.8f')} | {fmt(r['true_error'])} "
              f"| {fmt(r['error_estimate'])} | {r['n_evals']} "
              f"| {r['max_depth']} | — |")
        else:
            p(f"| {r['case']} | {r['method']} "
              f"| **失败** | — | {fmt(r['exact'], '.8f')} | — | — "
              f"| {r['n_evals']} | {r['max_depth']} "
              f"| {r['error_code']} |")

    p("\n**2b 整数周期**（真实积分恰为 0，所有采样点恰好落在函数零点）："
      "两种方法都只用十几次求值就返回——**答案偶然正确，但库内误差估计"
      "比真实舍入误差小十几个数量级**，不能作为可信的误差保证。"
      "这正是“误差估计失效”最隐蔽的形态：值碰巧对、估计却撒谎。\n")
    p("| k | 方法 | 状态 | 返回值 | 真实误差(=|值|) | 库内误差估计 | 求值数 |")
    p("|---:|---|---|---:|---:|---:|---:|")
    for r in data["oscillations"]:
        if not r["aligned"]:
            continue
        p(f"| {r['frequency_cycles']:g} | {r['method']} "
          f"| {'收敛' if r['converged'] else '失败'} "
          f"| {fmt(r['value'], '.3e')} | {fmt(r['true_error'])} "
          f"| {fmt(r['error_estimate'])} | {r['n_evals']} |")

    p("\n## 3. 误差估计失效区间（重点验收项）\n")
    p("### 3a Simpson 采样混叠\n")
    p("sin(16πx+0.3) 的周期与 Simpson 各层网格完全同相：Simpson 声称收敛、"
      "误差估计 ≈ 0，但真实答案为 0，返回值完全错误。Gauss-Legendre 不受影响。\n")
    p("| 方法 | 状态 | 返回值 | 真实误差 | 库内误差估计 | 求值数 |")
    p("|---|---|---:|---:|---:|---:|")
    for r in data["estimator_failure"]:
        if "混叠" in r["case"]:
            p(f"| {r['method']} | {'收敛' if r['converged'] else '失败'} "
              f"| {fmt(r['value'], '.6f')} | {fmt(r['true_error'])} "
              f"| {fmt(r['error_estimate'])} | {r['n_evals']} |")

    p("\n### 3b 端点处窄峰（两种方法均失效）\n")
    p("Lorentz 峰压在端点 x=0，精确积分 = 1/2 + arctan(1/eps)/π ≈ 1，"
      "但所有求积节点都落在峰外（Simpson 首层中点在 0.5；Gauss-Legendre "
      "最内侧节点距端点 ≈ 区间长度的 0.02），返回值停留在 0.5 且误差估计"
      "小至 1e-6~1e-9。**提高 max_depth/max_evals 无济于事**——这不是"
      "预算问题，而是节点族的结构性盲区；中心峰（第 5 节）则能被正常"
      "细化捕获。对策：已知峰位时在峰两侧分段积分，或做变量代换。\n")
    p("| 场景 | 方法 | 状态 | 返回值 | 精确值 | 真实误差 | 库内误差估计 | 求值数 | 失败码 |")
    p("|---|---|---|---:|---:|---:|---:|---:|---|")
    for r in data["estimator_failure"]:
        if "端点窄峰" in r["case"]:
            if r["converged"]:
                p(f"| {r['case']} | {r['method']} | 收敛 "
                  f"| {fmt(r['value'], '.6f')} | {fmt(r['exact'], '.6f')} "
                  f"| {fmt(r['true_error'])} | {fmt(r['error_estimate'])} "
                  f"| {r['n_evals']} | — |")
            else:
                p(f"| {r['case']} | {r['method']} | **失败** "
                  f"| — | {fmt(r['exact'], '.6f')} | — | — "
                  f"| {r['n_evals']} | {r['error_code']} |")

    p("\n## 4. 奇点：失败说明 + 正则化对照\n")
    p("| 场景 | 处理 | 状态 | 返回值/真实误差 | 位置 | 求值数 | 失败码 |")
    p("|---|---|---|---:|---|---:|---|")
    for r in data["singularities"]:
        if r["converged"]:
            if r["kind"] == "正则化":
                outcome = f"收敛；真实误差 {fmt(r.get('true_error'))}"
            else:
                outcome = f"收敛；值 {fmt(r.get('value'))}"
        else:
            outcome = "**失败，不返回数值**"
        p(f"| {r['case']} | {r['kind']} | {outcome} "
          f"| {r.get('position') or '—'} | {r['n_evals']} "
          f"| {r.get('error_code') or '—'} |")

    p("\n## 5. 中心窄峰：误差预算与深度上限\n")
    p("精确值 = 2·arctan(0.5/eps)/π。默认深度 20 对 eps≤1e-6 不足；"
      "深度 40 时全部成功（中点复用使求值数保持很少）。\n")
    p("| eps | 允许深度 | 状态 | 真实误差 | 库内误差估计 | 求值数 | 到达深度 | 失败码 |")
    p("|---:|---:|---|---:|---:|---:|---:|---|")
    for r in data["narrow_peaks"]:
        if r["converged"]:
            p(f"| {r['case'].split('=')[1]} | {r['max_depth_allowed']} "
              f"| 收敛 | {fmt(r['true_error'])} "
              f"| {fmt(r['error_estimate'])} | {r['n_evals']} "
              f"| {r['max_depth']} | — |")
        else:
            p(f"| {r['case'].split('=')[1]} | {r['max_depth_allowed']} "
              f"| **失败** | — | — | {r['n_evals']} | {r['max_depth']} "
              f"| {r['error_code']} |")

    p("\n## 结论\n")
    p("- 光滑函数上两种方法的真实误差均随请求容差下降；库内误差估计"
      "为经验外推量，通常为真实误差的数倍至数千倍（偏保守），但"
      "**在求积规则恰好精确命中被积函数结构时会远小于真实舍入误差**，"
      "因此它不是严格误差上界；")
    p("- 非整数倍频高振荡：频率升高使成本快速增长，超出深度/预算时返回"
      "`failed`（MAX_DEPTH/EVAL_BUDGET），不静默给值；")
    p("- **误差估计可能失效的三个区间**（本研究实测）：")
    p("  1. Simpson 采样混叠（3a，sin(16πx+0.3)）——Gauss 可救；")
    p("  2. 节点族端点盲区造成的端点窄峰（3b）——两种规则均失效，"
      "需分段积分或变量代换，加预算无效；")
    p("  3. 整数周期振荡（2b）——返回值偶然正确但误差估计过度乐观"
      "十几个数量级；")
    p("- 所有端点奇异/内部极点均以 SINGULAR_* 失败码返回，给出位置与"
      "处理建议，不返回数值；变量代换/补极限值正则化后可正常积分；")
    p("- 中心窄峰（第 5 节）展示误差预算递归分配的正常行为：深度 20 "
      "不足时失败，深度 40 成功且借助中点复用保持很少的求值数。")
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stdout", action="store_true",
                    help="只打印 Markdown，不写 results/ 文件")
    args = ap.parse_args()

    data = {
        "sweep": study_tolerance_sweep(),
        "oscillations": study_oscillations(),
        "estimator_failure": study_estimator_failure(),
        "singularities": study_singularities(),
        "narrow_peaks": study_narrow_peaks(),
    }
    md = render_markdown(data)
    print(md)

    if not args.stdout:
        os.makedirs(RESULTS_DIR, exist_ok=True)
        with open(os.path.join(RESULTS_DIR, "acceptance_results.json"),
                  "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        with open(os.path.join(RESULTS_DIR, "acceptance_report.md"),
                  "w", encoding="utf-8") as fh:
            fh.write(md)
        print(f"\n[已写入 {RESULTS_DIR}/acceptance_report.md 与 "
              f"acceptance_results.json]", file=sys.stderr)


if __name__ == "__main__":
    main()
