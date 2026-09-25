"""命令行入口（纯后端，无界面）。

子命令
------
detect       按 JSON 请求文件或命令行参数对单个信号检测，输出数值文件
acceptance   运行合成验收场景（阶跃/漂移/尖峰/干净噪声），报告延迟与误报，
             并校验逐块与逐点输出完全一致
generate-pcm 生成示例 PCM 文件，供 detect 读取演示

所有结果写到输出目录：``*_points.csv``、``*_summary.json``、``*_marks.pcm``。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

from .detector import Decision, DetectorConfig, MissingPolicy
from .evaluation import LabeledEvent, evaluate
from .processing import (
    assert_results_equal,
    detect_blocks,
    detect_signal,
    vectorized_detect,
)
from .reporting import write_marks_pcm, write_points_csv, write_summary_json
from .signal_io import (
    inject_missing,
    make_scenario,
    read_pcm,
    write_pcm,
)

# 验收场景的默认参数（与 make_scenario 默认值一致，显式写出便于复现）。
ACCEPTANCE_SCENARIOS = {
    "step": dict(step_at=1000, step_amp=6.0),
    # 0.2 sigma/点的快漂移可在数百点内累积到阈值以上；
    # 更慢的漂移会被稳健窗口自适应（见 README“已知局限”）。
    "drift": dict(drift_at=800, drift_rate=0.2),
    "spike": dict(spike_positions=(600, 1200, 1700), spike_amp=8.0),
    "clean": {},
}
DEFAULT_SIGNAL_KW = dict(n=2000, sigma=1.0, seed=0)


def default_block_sizes(n: int) -> list[int]:
    """构造一个必然覆盖 n 个样本的不规则切块用于一致性演示。"""
    sizes: list[int] = []
    start = 0
    for c in (37, 256, 512):
        if start + c < n:
            sizes.append(c)
            start += c
    sizes.append(n - start)
    return sizes


def _config_from_dict(cfg: dict | None) -> DetectorConfig:
    cfg = dict(cfg or {})
    if "missing_policy" in cfg:
        cfg["missing_policy"] = MissingPolicy(str(cfg["missing_policy"]))
    return DetectorConfig(**cfg)


def _load_signal(spec: dict) -> tuple[np.ndarray, str]:
    """根据请求中的 input 段构造信号，返回 (信号, 来源描述)。"""
    if "scenario" in spec:
        kind = spec["scenario"]
        kw = {k: v for k, v in spec.items()
              if k not in ("scenario", "missing_positions")}
        if "spike_positions" in kw:
            kw["spike_positions"] = tuple(kw["spike_positions"])
        signal = make_scenario(kind, **kw)
        # 可选：在合成信号指定位置注入缺样（NaN），用于演示缺样策略。
        positions = spec.get("missing_positions")
        if positions:
            signal = inject_missing(signal, positions)
        return signal, f"synthetic:{kind}"
    if "pcm" in spec:
        return (
            read_pcm(
                spec["pcm"],
                dtype=spec.get("dtype", "int16"),
                channels=int(spec.get("channels", 1)),
                channel=spec.get("channel", 0),
                big_endian=bool(spec.get("big_endian", False)),
                normalize=bool(spec.get("normalize", True)),
            ),
            f"pcm:{spec['pcm']}",
        )
    raise ValueError("input 必须包含 scenario 或 pcm 字段")


def _events_from_dict(events) -> tuple[LabeledEvent, ...]:
    return tuple(
        LabeledEvent(kind=str(e["kind"]), start=int(e["start"]),
                     eval_end=int(e["eval_end"]))
        for e in (events or [])
    )


# ----------------------------------------------------------------- detect
def run_detect(request: dict) -> dict:
    """执行一次检测请求，返回产物路径与控制台摘要。"""
    config = _config_from_dict(request.get("config"))
    signal, source = _load_signal(request["input"])
    out_dir = Path(request.get("output_dir", "out"))
    name = request.get("name", "signal")
    events = _events_from_dict(request.get("events"))

    # 逐点参考结果。
    result = detect_signal(signal, config)

    # 逐块结果（若请求给定切块；否则用不规则默认切块演示）并严格比对。
    blocks = request.get("blocks")
    consistency = None
    if blocks is not None or request.get("check_blocks", True):
        if blocks is None:
            blocks = default_block_sizes(signal.size)
        block_result = detect_blocks(signal, config, blocks)
        assert_results_equal(result, block_result)
        consistency = {
            "equal": True,
            "n": int(result.decisions.size),
            "block_sizes": [int(b) for b in blocks],
        }

    eval_report = evaluate(result, events) if events else None

    paths = {
        "points_csv": write_points_csv(out_dir / f"{name}_points.csv", result),
        "summary_json": write_summary_json(
            out_dir / f"{name}_summary.json", result, config,
            source=source, eval_report=eval_report,
        ),
        "marks_pcm": write_marks_pcm(out_dir / f"{name}_marks.pcm", result),
    }
    console = [
        f"[{name}] 来源={source} 样本数={signal.size}",
        f"  异常点 {int(result.is_anomaly.sum())}，"
        f"缺样 {result.missing_indices.size}，预热 {result.warmup_indices.size}",
        f"  异常索引: {result.anomaly_indices.tolist()[:20]}"
        + (" ..." if result.anomaly_indices.size > 20 else ""),
    ]
    if eval_report is not None:
        console += ["  " + line for line in eval_report.summary_lines()]
    if consistency is not None:
        console.append(
            f"  逐块一致性: 通过（{len(consistency['block_sizes'])} 块，"
            f"逐点完全相等）"
        )
    return {"paths": paths, "console": console}


def _assert_equal(a, b) -> dict:
    """委托给 processing.assert_results_equal（保留兼容封装）。"""
    assert_results_equal(a, b)
    return {"equal": True, "n": int(a.decisions.size)}


# ------------------------------------------------------------- acceptance
def run_acceptance(out_root: str = "out/acceptance") -> dict:
    """合成验收：阶跃、漂移、孤立尖峰、干净噪声；报告延迟与误报。"""
    out_root = Path(out_root)
    config = DetectorConfig()  # 默认参数：w=200, z>6, warmup=30
    all_lines = []
    scenarios_report = {}

    for kind, extra in ACCEPTANCE_SCENARIOS.items():
        signal = make_scenario(kind, **DEFAULT_SIGNAL_KW, **extra)
        result = detect_signal(signal, config)
        vec = vectorized_detect(signal, config)
        _assert_equal(result, vec)  # 交叉验证流式与独立向量化实现

        # 不规则切块一致性。
        n = signal.size
        blocks = [1, 7, 64, 100, 256, n - (1 + 7 + 64 + 100 + 256)]
        block_result = detect_blocks(signal, config, blocks)
        _assert_equal(result, block_result)

        # 标注事件（评估窗只向事件之后开放）。
        if kind == "step":
            events = [LabeledEvent("step", 1000, 1200)]
        elif kind == "drift":
            events = [LabeledEvent("drift", 800, 1400)]
        elif kind == "spike":
            events = [
                LabeledEvent("spike", p, min(p + 5, n))
                for p in (600, 1200, 1700)
            ]
        else:
            events = []
        report = evaluate(result, events)

        write_points_csv(out_root / f"{kind}_points.csv", result)
        write_marks_pcm(out_root / f"{kind}_marks.pcm", result)
        write_summary_json(
            out_root / f"{kind}_summary.json", result, config,
            source=f"synthetic:{kind}", eval_report=report,
        )
        scenarios_report[kind] = {
            "delays": list(report.delays),
            "n_detected": report.n_detected,
            "n_missed": report.n_missed,
            "false_alarm_points": report.false_alarm_points,
            "false_alarm_rate": report.false_alarm_rate,
            "anomaly_count": int(result.is_anomaly.sum()),
        }
        all_lines.append(f"== 场景 {kind} ==")
        all_lines.append(
            f"   异常判决点 {int(result.is_anomaly.sum())}"
        )
        if events:
            all_lines += ["   " + l for l in report.summary_lines()]
        else:
            all_lines.append(
                f"   干净噪声虚警: {report.false_alarm_points} 点 / "
                f"{report.eligible_points} 可判决点 = "
                f"{report.false_alarm_rate:.6%}"
            )
        all_lines.append("   逐块一致性: 通过（块大小 [1,7,64,100,256,余量]）")
        all_lines.append("   流式 vs 独立向量化实现: 逐点一致")

    (out_root / "acceptance_report.json").write_text(
        json.dumps(scenarios_report, indent=2, ensure_ascii=False)
    )
    return {"console": all_lines, "report": scenarios_report,
            "out_dir": str(out_root)}


# ---------------------------------------------------------- generate-pcm
def run_generate_pcm(path: str, *, kind: str = "spike",
                     dtype: str = "int16") -> str:
    signal = make_scenario(kind)
    n = write_pcm(path, signal * 0.1, dtype=dtype)  # 缩小幅度避免量化溢出
    return f"已写入 {path}：{n} 个 {dtype} 样本（场景 {kind}）"


# ---------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="burst-detector",
        description="突发异常信号检测（离线、纯后端、NumPy）",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pd = sub.add_parser("detect", help="按请求 JSON 执行一次检测")
    pd.add_argument("request", nargs="?", help="请求 JSON 路径；缺省从 stdin 读取")
    pd.add_argument("--output-dir", help="覆盖请求中的 output_dir")

    pa = sub.add_parser("acceptance", help="运行合成验收场景")
    pa.add_argument("--out", default="out/acceptance")

    pg = sub.add_parser("generate-pcm", help="生成示例 PCM 文件")
    pg.add_argument("path")
    pg.add_argument("--kind", default="spike",
                    choices=["clean", "step", "drift", "spike"])
    pg.add_argument("--dtype", default="int16")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "detect":
        if args.request:
            request = json.loads(Path(args.request).read_text())
        else:
            request = json.loads(sys.stdin.read())
        if args.output_dir:
            request["output_dir"] = args.output_dir
        out = run_detect(request)
        print("\n".join(out["console"]))
        print("产物:")
        for k, v in out["paths"].items():
            print(f"  {k}: {v}")
        return 0
    if args.cmd == "acceptance":
        out = run_acceptance(args.out)
        print("\n".join(out["console"]))
        print(f"\n汇总报告: {Path(out['out_dir']) / 'acceptance_report.json'}")
        return 0
    if args.cmd == "generate-pcm":
        print(run_generate_pcm(args.path, kind=args.kind, dtype=args.dtype))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
