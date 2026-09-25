"""验收脚本：在可解析函数上比较两种自适应方法的真实误差与误差估计。

运行：
    python3 scripts/acceptance.py
输出：
    控制台表格；并写入 examples/acceptance_report.md 与
    examples/acceptance_results.json（机器可读）。

覆盖：
  A. 解析可查函数（多项式、指数、三角函数、高斯、对数、反正切）
  B. 高振荡 sin(omega*x)，omega 扫描
  C. 端点奇异 / 内部极点 / 不可积奇点
  D. 窄高斯峰（宽度与峰位扫描）
  E. 误差估计失效范围的显式标注（真实误差 / 估计 > 100 或状态误判）
"""

from __future__ import annotations

import json
import math
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adaptive_integration import integrate  # noqa: E402

REPO = Path(__file__).resolve().parents[1]

FAIL_CODES_OK = {
    "DEPTH_LIMIT_REACHED",
    "EVALUATION_BUDGET_EXHAUSTED",
    "INVALID_VALUE_AT_POINT",
    "ROUND_OFF_NO_PROGRESS",
}


@dataclass
class Row:
    section: str
    case: str
    method: str
    status: str
    value: Optional[float]
    exact: Optional[float]
    true_error: Optional[float]
    error_estimate: Optional[float]
    overconfidence: Optional[float]   # true_error / estimate
    evaluations: int
    depth: int
    intervals: int
    error_code: Optional[str]
    note: str = ""


def _row(section, case, method, result, exact, note="") -> Row:
    if result.converged and exact is not None:
        te = abs(float(result.value) - exact)
        est = result.error_estimate
        ov = te / est if est and est > 0 else (math.inf if te > 1e-12 else 0.0)
    else:
        te = None
        ov = None
    return Row(
        section=section,
        case=case,
        method=method,
        status=result.status,
        value=None if result.value is None else float(result.value),
        exact=exact,
        true_error=None if te is None else float(te),
        error_estimate=(None if result.error_estimate is None
                        else float(result.error_estimate)),
        overconfidence=None if ov is None else float(ov),
        evaluations=result.evaluations,
        depth=result.depth_reached,
        intervals=result.intervals,
        error_code=result.error_code,
        note=note,
    )


def gaussian_exact(x0, delta, lo=0.0, hi=1.0):
    return 0.5 * (math.erf((hi - x0) / delta) - math.erf((lo - x0) / delta))


def main() -> int:
    rows: list[Row] = []

    # ------------------------------------------------------------------ A
    analytic = [
        ("x^3+2x+1", "x^3 + 2*x + 1", 0.0, 1.0, 2.25),
        ("exp(x) on [0,1]", "exp(x)", 0.0, 1.0, math.e - 1),
        ("4/(1+x^2) -> pi", "4/(1+x^2)", 0.0, 1.0, math.pi),
        ("sin(x) on [0,pi]", "sin(x)", 0.0, math.pi, 2.0),
        ("exp(-x^2) on [-1,1]", "exp(-x^2)", -1.0, 1.0,
         math.sqrt(math.pi) * math.erf(1)),
        ("log(x) on [1,2]", "log(x)", 1.0, 2.0,
         2.0 * math.log(2) - 1.0),
        ("1/(1+x^2) on [-5,5]", "1/(1+x^2)", -5.0, 5.0,
         2.0 * math.atan(5.0)),
        ("cos(10x) on [0,1]", "cos(10*x)", 0.0, 1.0, math.sin(10.0) / 10.0),
    ]
    for label, expr, a, b, exact in analytic:
        for method in ("gk15", "simpson"):
            r = integrate(expr, a, b, method=method,
                          abs_tol=1e-10, rel_tol=1e-10)
            rows.append(_row("A 解析函数", label, method, r, exact))

    # ------------------------------------------------------------------ B
    for omega in (100, 500, 1000, 5000):
        expr = f"sin({omega}*x)"
        exact = (1.0 - math.cos(omega)) / omega
        for method in ("gk15", "simpson"):
            r = integrate(expr, 0.0, 1.0, method=method,
                          abs_tol=1e-10, rel_tol=1e-10)
            note = ""
            if r.converged and abs(r.value - exact) > 1e-6:
                note = "高置信度错值：等距节点混叠（误差估计失效）"
            rows.append(_row("B 高振荡", f"sin({omega}x)",
                             method, r, exact, note))

    # 松容差下 Simpson 对 sin(100x) 的等距节点混叠（误报收敛的典型）
    exact_100 = (1.0 - math.cos(100)) / 100
    r = integrate("sin(100*x)", 0.0, 1.0, method="simpson",
                  abs_tol=1e-10, rel_tol=1e-8)
    note = ""
    if r.converged and abs(r.value - exact_100) > 0.1:
        note = ("Simpson 粗面板两级估计偶然一致 -> 提前停止；"
                "收紧 rel_tol 或改用 gk15 可消除")
    rows.append(_row("B 高振荡(失效展示)",
                     "sin(100x) Simpson 松容差",
                     "simpson", r, exact_100, note))
    r = integrate("sin(100*x)", 0.0, 1.0, method="simpson",
                  abs_tol=1e-10, rel_tol=0.0)
    rows.append(_row("B 高振荡(对照)",
                     "sin(100x) Simpson rel_tol=0",
                     "simpson", r, exact_100,
                     "收紧容差后正确（代价是更多求值）"))

    # ------------------------------------------------------------------ C
    singularity_cases = [
        ("端点弱奇异 1/sqrt(x)", "1/sqrt(x)", 0.0, 1.0, 2.0),
        ("端点 x*log(x)", "x*log(x)", 0.0, 1.0, -0.25),
        ("端点 log(x)", "log(x)", 0.0, 1.0, -1.0),
        ("内部极点 1/x", "1/x", -1.0, 1.0, None),
        ("不可积 1/x^2", "1/x^2", 0.0, 1.0, None),
    ]
    for label, expr, a, b, exact in singularity_cases:
        for method in ("gk15", "simpson"):
            r = integrate(expr, a, b, method=method,
                          abs_tol=1e-8, rel_tol=1e-8)
            rows.append(_row("C 奇点", label, method, r, exact))

    # ------------------------------------------------------------------ D
    for x0 in (0.5, 0.37):
        for delta in (0.01, 0.003, 0.001, 1e-4):
            expr = (f"exp(-((x-{x0})/{delta})^2)"
                    f"/(sqrt(pi)*{delta})")
            exact = gaussian_exact(x0, delta)
            r = integrate(expr, 0.0, 1.0, abs_tol=1e-8, rel_tol=1e-8)
            note = ""
            if r.converged and abs(r.value - exact) > 0.5:
                note = "峰未被任何节点采样：规则同时漏检，误报收敛≈0"
            rows.append(_row("D 窄峰(默认单面板)",
                             f"x0={x0}, delta={delta:g}",
                             "gk15", r, exact, note))

    # 救援手段
    rescue = [
        ("delta=1e-4, x0=.37: initial_intervals=2000",
         "exp(-((x-0.37)/1e-4)^2)/(sqrt(pi)*1e-4)",
         {"initial_intervals": 2000}, gaussian_exact(0.37, 1e-4)),
        ("delta=1e-4, x0=.37: points 包围峰",
         "exp(-((x-0.37)/1e-4)^2)/(sqrt(pi)*1e-4)",
         {"points": [0.36, 0.38]}, gaussian_exact(0.37, 1e-4)),
        ("delta=1e-4, x0=.37: Simpson 加密网格",
         "exp(-((x-0.37)/1e-4)^2)/(sqrt(pi)*1e-4)",
         {"method": "simpson", "initial_intervals": 2000},
         gaussian_exact(0.37, 1e-4)),
    ]
    for label, expr, kw, exact in rescue:
        r = integrate(expr, 0.0, 1.0, abs_tol=1e-8, rel_tol=1e-8, **kw)
        rows.append(_row("D 窄峰(救援)", label, kw.get("method", "gk15"),
                         r, exact))

    # ------------------------------------------------------------------ E
    # 极限容差 / 深度限制行为（结构化失败）
    limits = [
        ("1/sqrt(x), max_depth=5", "1/sqrt(x)", {"max_depth": 5}, 2.0),
        ("sin(1000x), 预算 150", "sin(1000*x)",
         {"max_evaluations": 150}, (1 - math.cos(1000)) / 1000),
    ]
    for label, expr, kw, exact in limits:
        r = integrate(expr, 0.0, 1.0, abs_tol=1e-12, rel_tol=0.0, **kw)
        rows.append(_row("E 限制与失败", label,
                         kw.get("method", "gk15"), r, exact))

    # ------------------------------------------------------------- 报告
    lines: list[str] = []
    cur = None
    for row in rows:
        if row.section != cur:
            cur = row.section
            lines.append(f"\n### {cur}")
            lines.append(
                f"{'用例':<40} {'方法':<7} {'状态':<9} "
                f"{'值':>14} {'真实误差':>11} {'误差估计':>11} "
                f"{'比值':>9} {'求值':>6} {'深度':>4}"
            )
        te = "—" if row.true_error is None else f"{row.true_error:.2e}"
        est = "—" if row.error_estimate is None else (
            f"{row.error_estimate:.2e}")
        val = "—" if row.value is None else f"{row.value: .6e}"
        ov = "—" if row.overconfidence is None else (
            "inf" if math.isinf(row.overconfidence)
            else f"{row.overconfidence:.1e}")
        lines.append(
            f"{row.case:<40} {row.method:<7} {row.status:<9} "
            f"{val:>14} {te:>11} {est:>11} {ov:>9} "
            f"{row.evaluations:>6} {row.depth:>4}"
            + (f"  -> {row.error_code}" if row.error_code else "")
            + (f"  [{row.note}]" if row.note else "")
        )

    report = "\n".join(lines)
    print(report)

    # 汇总
    n_conv = sum(1 for r in rows if r.status == "converged")
    n_fail = sum(1 for r in rows if r.status == "failed")
    silent_bad = [
        r for r in rows
        if r.status == "converged"
        and r.true_error is not None
        and r.true_error > max(1e-6,
                               100.0 * (r.error_estimate or 0.0))
    ]
    honest_fail = [
        r for r in rows
        if r.status == "failed"
        and r.error_code in FAIL_CODES_OK
    ]

    summary = (
        f"\n汇总: {len(rows)} 个用例；收敛 {n_conv}，结构化失败 {n_fail}；"
        f"其中误差估计失效（高置信度错值）{len(silent_bad)} 个；"
        f"诚实失败 {len(honest_fail)} 个。\n"
    )
    print(summary)

    out_json = REPO / "examples" / "acceptance_results.json"
    out_json.write_text(
        json.dumps([asdict(r) for r in rows],
                   ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    out_md = REPO / "examples" / "acceptance_report.md"
    out_md.write_text(
        "# 自适应积分验收结果（scripts/acceptance.py 实际运行输出）\n"
        + "```\n" + report + "\n" + summary + "```\n\n"
        + "## 误差估计失效用例（库会误报收敛，需调用方规避）\n"
        + "".join(
            f"- {r.section} / {r.case} / {r.method}: "
            f"值={r.value:.4e}, 真实误差={r.true_error:.2e}, "
            f"估计={r.error_estimate:.2e}。{r.note}\n"
            for r in silent_bad
        )
        + "\n## 诚实失败用例（不收敛/奇点 -> 明确错误码）\n"
        + "".join(
            f"- {r.section} / {r.case} / {r.method}: {r.error_code}\n"
            for r in honest_fail
        ),
        encoding="utf-8",
    )

    print(f"已写入 {out_json.relative_to(REPO)} 与 "
          f"{out_md.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
