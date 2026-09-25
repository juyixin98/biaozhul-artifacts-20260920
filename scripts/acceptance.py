"""验收脚本：合成离群数据上 Huber IRLS 对照 OLS。

覆盖：
  1. 离群点：Huber 斜率/截距比 OLS 更接近真值；目标单调下降；权重压低离群点
  2. 共线列：秩亏显式上报（rank_deficient + warning），预测仍精确
  3. 全零残差：δ="auto" 零散布路径，目标为 0 且收敛
  4. 可复现性：同一输入两次拟合逐位一致；δ 极大时 Huber≈OLS

运行：
    python scripts/acceptance.py            # 文本报告输出到 stdout
    python scripts/acceptance.py --json     # 同时写 machine-readable 结果
退出码：全部检查通过 0，否则 1。
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from robust_regression import fit_from_json, fit_huber, fit_ols  # noqa: E402
from robust_regression.huber import huber_weights  # noqa: E402

SEED = 1
DELTA = 0.5
N_OUTLIERS = 4
OUTLIER_SHIFT = 8.0

checks: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    checks.append((name, bool(condition), detail))


def section(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--json",
        dest="json_out",
        default=None,
        help="把机器可读结果写入给定 JSON 文件",
    )
    args = parser.parse_args()

    report: dict = {
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "numpy": np.__version__,
            "seed": SEED,
        },
        "sections": {},
    }

    # ------------------------------------------------------------------
    section("1. 合成离群数据：Huber vs OLS")
    rng = np.random.default_rng(SEED)
    n = 30
    x = np.linspace(0.0, 10.0, n)
    x = x - x.mean()  # 中心化：斜率与截距估计解耦
    y_clean = 2.0 * x + 1.0
    noise = rng.normal(scale=0.3, size=n)
    y = y_clean + noise
    # 4 个中部垂直离群点，符号交错（整体常数项近似抵消）
    outlier_idx = np.linspace(4, n - 5, N_OUTLIERS).astype(int)
    y[outlier_idx] += OUTLIER_SHIFT * np.array(
        [1.0, -1.0, 1.0, -1.0]
    )
    X = x.reshape(-1, 1)

    ols = fit_ols(X, y, fit_intercept=True)
    hub = fit_huber(
        X, y, delta=DELTA, reg_lambda=0.0, tol=1e-10, max_iter=300
    )

    print(f"真实参数:        slope=2.000000, intercept=1.000000")
    print(
        f"OLS 估计:        slope={ols['coefficients'][0]:.6f}, "
        f"intercept={ols['intercept']:.6f}, SSE={ols['objective_sse']:.6f}"
    )
    print(
        f"Huber 估计:      slope={hub['coefficients'][0]:.6f}, "
        f"intercept={hub['intercept']:.6f}, "
        f"objective={hub['objective']:.6f}"
    )
    print(
        f"Huber 收敛:      {hub['status']} "
        f"(iterations={hub['iterations']}, delta_used={hub['delta_used']})"
    )
    r_hub = y - X[:, 0] * hub["coefficients"][0] - hub["intercept"]
    w_hub = huber_weights(r_hub, DELTA)
    print(
        f"离群点平均权重:  {w_hub[outlier_idx].mean():.4f} "
        f"(干净点平均权重: {np.delete(w_hub, outlier_idx).mean():.4f})"
    )

    hist = np.array(hub["objective_history"])
    increases = np.diff(hist)[np.diff(hist) > 1e-10 * (1 + np.abs(hist[:-1]))]
    print(
        f"目标轨迹:        初值={hist[0]:.6f}, 终值={hist[-1]:.6f}, "
        f"长度={len(hist)}, 超过容差的上升次数={len(increases)}"
    )

    check("Huber 收敛", hub["status"] == "converged", hub["status"])
    check(
        "Huber 斜率误差 < 0.1",
        abs(hub["coefficients"][0] - 2.0) < 0.1,
        f"err={abs(hub['coefficients'][0] - 2.0):.4f}",
    )
    check(
        "Huber 截距误差 < 0.1",
        abs(hub["intercept"] - 1.0) < 0.1,
        f"err={abs(hub['intercept'] - 1.0):.4f}",
    )
    check(
        "Huber 比 OLS 更接近真实斜率",
        abs(hub["coefficients"][0] - 2.0)
        < abs(ols["coefficients"][0] - 2.0),
        f"huber_err={abs(hub['coefficients'][0] - 2.0):.4f} "
        f"ols_err={abs(ols['coefficients'][0] - 2.0):.4f}",
    )
    check(
        "Huber 比 OLS 更接近真实截距",
        abs(hub["intercept"] - 1.0)
        < abs(ols["intercept"] - 1.0),
        f"huber_err={abs(hub['intercept'] - 1.0):.4f} "
        f"ols_err={abs(ols['intercept'] - 1.0):.4f}",
    )
    check(
        "目标值相对 OLS 初值下降",
        hist[-1] < hist[0],
        f"{hist[0]:.6f} -> {hist[-1]:.6f}",
    )
    check(
        "目标轨迹单调不增（容差 1e-10）",
        len(increases) == 0,
        f"increases={len(increases)}",
    )
    check(
        "离群点权重被压低 (<0.1)，干净点权重接近 1 (>0.95)",
        w_hub[outlier_idx].mean() < 0.1
        and np.delete(w_hub, outlier_idx).mean() > 0.95,
        f"out={w_hub[outlier_idx].mean():.4f}, "
        f"clean={np.delete(w_hub, outlier_idx).mean():.4f}",
    )
    report["sections"]["outliers"] = {
        "true": {"slope": 2.0, "intercept": 1.0},
        "ols": {
            "slope": ols["coefficients"][0],
            "intercept": ols["intercept"],
            "sse": ols["objective_sse"],
        },
        "huber": {
            "slope": hub["coefficients"][0],
            "intercept": hub["intercept"],
            "objective": hub["objective"],
            "status": hub["status"],
            "iterations": hub["iterations"],
            "objective_history": hub["objective_history"],
        },
    }

    # ------------------------------------------------------------------
    section("2. 共线列（秩亏显式处理）")
    xc = np.linspace(0.0, 5.0, 20)
    Xc = np.column_stack([xc, xc, 2.0 * xc])
    yc = 4.0 * xc + 0.5
    ols_c = fit_ols(Xc, yc, fit_intercept=True)
    hub_c = fit_huber(
        Xc, yc, delta=1.0, tol=1e-10, max_iter=300
    )
    pred = Xc @ np.array(hub_c["coefficients"]) + hub_c["intercept"]
    pred_err = float(np.max(np.abs(pred - yc)))
    print(f"OLS:  rank={ols_c['rank']}, rank_deficient={ols_c['rank_deficient']}")
    print(
        f"Huber: rank={hub_c['rank']}, "
        f"rank_deficient={hub_c['rank_deficient']}, "
        f"warnings={hub_c['warnings']}, status={hub_c['status']}"
    )
    print(f"最大预测误差: {pred_err:.2e}")
    check("OLS 上报秩亏", ols_c["rank_deficient"], f"rank={ols_c['rank']}")
    check(
        "Huber 上报秩亏并给出 warning",
        hub_c["rank_deficient"]
        and "rank_deficient_min_norm_solution" in hub_c["warnings"],
        f"rank={hub_c['rank']}, warnings={hub_c['warnings']}",
    )
    check(
        "秩亏下预测仍精确 (<1e-6)",
        pred_err < 1e-6,
        f"max_err={pred_err:.2e}",
    )
    check(
        "秩亏下仍然收敛",
        hub_c["status"] == "converged",
        hub_c["status"],
    )
    report["sections"]["collinear"] = {
        "rank": hub_c["rank"],
        "rank_deficient": hub_c["rank_deficient"],
        "warnings": hub_c["warnings"],
        "max_prediction_error": pred_err,
        "status": hub_c["status"],
    }

    # ------------------------------------------------------------------
    section("3. 全零残差（δ=auto 零散布路径）")
    Xz = np.arange(1, 11, dtype=np.float64).reshape(-1, 1)
    yz = 3.0 * Xz[:, 0] - 2.0
    hub_z = fit_huber(Xz, yz, delta="auto", fit_intercept=True, tol=1e-12)
    print(f"status={hub_z['status']}, warnings={hub_z['warnings']}")
    print(
        f"coef={hub_z['coefficients']}, intercept={hub_z['intercept']}, "
        f"objective={hub_z['objective']:.3e}, iterations={hub_z['iterations']}"
    )
    check(
        "全零残差收敛",
        hub_z["status"] == "converged",
        hub_z["status"],
    )
    check(
        "全零残差目标为 0",
        abs(hub_z["objective"]) < 1e-10,
        f"objective={hub_z['objective']:.3e}",
    )
    check(
        "零散布 warning 与参数精确恢复",
        "auto_scale_all_zero_residuals" in hub_z["warnings"]
        and abs(hub_z["coefficients"][0] - 3.0) < 1e-8
        and abs(hub_z["intercept"] + 2.0) < 1e-8,
        str(hub_z["warnings"]),
    )
    report["sections"]["zero_residual"] = {
        "status": hub_z["status"],
        "warnings": hub_z["warnings"],
        "objective": hub_z["objective"],
        "coefficients": hub_z["coefficients"],
        "intercept": hub_z["intercept"],
    }

    # ------------------------------------------------------------------
    section("4. 可复现性 / δ→∞ 等价 OLS / JSON 接口")
    r1 = fit_huber(X, y, delta=DELTA, tol=1e-10, max_iter=300)
    r2 = fit_huber(X, y, delta=DELTA, tol=1e-10, max_iter=300)
    bitwise = (
        r1["objective_history"] == r2["objective_history"]
        and r1["coefficients"] == r2["coefficients"]
        and r1["intercept"] == r2["intercept"]
    )
    print(f"两次拟合逐位一致: {bitwise}")
    check("两次拟合逐位一致（确定性）", bitwise)

    rng2 = np.random.default_rng(99)
    Xb = rng2.normal(size=(60, 2))
    yb = Xb @ np.array([1.0, -1.5]) + 0.5 + rng2.normal(scale=0.1, size=60)
    hb = fit_huber(Xb, yb, delta=1e6, tol=1e-12, max_iter=500)
    ob = fit_ols(Xb, yb)
    coef_err = float(
        np.max(np.abs(np.array(hb["coefficients"]) - np.array(ob["coefficients"])))
    )
    int_err = abs(hb["intercept"] - ob["intercept"])
    print(
        f"δ=1e6 时 Huber 与 OLS 系数最大差: {coef_err:.2e}, "
        f"截距差: {int_err:.2e}"
    )
    check(
        "δ 极大时 Huber≈OLS (<1e-5)",
        coef_err < 1e-5 and int_err < 1e-5,
        f"coef_err={coef_err:.2e}, int_err={int_err:.2e}",
    )

    payload = {
        "X": X.tolist(),
        "y": y.tolist(),
        "delta": DELTA,
        "tol": 1e-10,
        "max_iter": 300,
    }
    jr = fit_from_json(payload)
    check(
        "JSON 接口 ok=true 且与库调用目标一致",
        jr["ok"]
        and abs(jr["result"]["objective"] - r1["objective"]) < 1e-12,
        f"json={jr['result']['objective']:.10f} "
        f"lib={r1['objective']:.10f}",
    )
    jerr = fit_from_json({"method": "huber", "X": [[1.0]], "y": [1.0, 2.0]})
    check(
        "JSON 接口形状错误返回 input_error",
        (not jerr["ok"]) and jerr["error"]["type"] == "input_error",
        jerr.get("error", {}).get("message", ""),
    )
    report["sections"]["reproducibility"] = {
        "bitwise_identical": bitwise,
        "large_delta_vs_ols": {
            "max_coef_error": coef_err,
            "intercept_error": int_err,
        },
        "json_ok": jr["ok"],
    }

    # ------------------------------------------------------------------
    section("检查结果汇总")
    n_pass = sum(1 for _, ok_, _ in checks if ok_)
    for name, ok_, detail in checks:
        mark = "PASS" if ok_ else "FAIL"
        suffix = f"  [{detail}]" if detail else ""
        print(f"  [{mark}] {name}{suffix}")
    print(f"\n通过 {n_pass}/{len(checks)}")

    report["summary"] = {
        "passed": n_pass,
        "total": len(checks),
        "all_passed": n_pass == len(checks),
        "results": [
            {"name": name, "passed": ok_, "detail": detail}
            for name, ok_, detail in checks
        ],
    }
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
            + "\n",
            encoding="utf-8",
        )
        print(f"\n机器可读结果已写入: {args.json_out}")

    return 0 if n_pass == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
