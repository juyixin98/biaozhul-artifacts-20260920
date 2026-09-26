"""命令行入口（离线批处理，无服务、无前端）。

用法::

    python -m pose_graph_optimizer.cli solve -r request.json [-o response.json] [-v]
    python -m pose_graph_optimizer.cli demo -s square [-o response.json]
    python -m pose_graph_optimizer.cli list-scenarios
"""

from __future__ import annotations

import argparse
import sys

import numpy as np

from .io_json import (
    ParseError,
    dump_response,
    graph_error_to_dict,
    load_request,
    result_to_dict,
)
from .optimizer import GraphNotSolvedError, OptimizerOptions, optimize
from .se2 import wrap_angle
from .synthetic import standard_scenarios


def _cmd_solve(args: argparse.Namespace) -> int:
    try:
        graph, options, name = load_request(args.request)
    except ParseError as exc:
        print(f"请求解析失败: {exc}", file=sys.stderr)
        return 2
    options.verbose = args.verbose or options.verbose
    try:
        result = optimize(graph, options)
    except GraphNotSolvedError as exc:
        response = graph_error_to_dict(graph, [str(exc)], name)
        text = dump_response(response, args.output)
        if args.output is None:
            print(text)
        print(f"图结构诊断失败: {exc}", file=sys.stderr)
        return 1

    response = result_to_dict(result, graph, name)
    text = dump_response(response, args.output)
    if args.output is None:
        print(text)

    summary = result.to_summary()
    print(
        f"状态: {summary['status']}（{result.message}）| "
        f"迭代 {result.iterations} 次 | "
        f"代价 {summary['initial_cost']:.6f} -> {summary['final_cost']:.6f} "
        f"(下降 {summary['cost_reduction_ratio'] * 100:.2f}%)",
        file=sys.stderr,
    )
    return 0 if result.success else 3


def _cmd_demo(args: argparse.Namespace) -> int:
    scenarios = standard_scenarios(seed=args.seed)
    if args.scenario not in scenarios:
        print(
            f"未知场景 {args.scenario!r}，可选: {', '.join(scenarios)}",
            file=sys.stderr,
        )
        return 2
    spec = scenarios[args.scenario]
    graph, truth = spec["builder"]()
    print(f"场景: {args.scenario} —— {spec['description']}", file=sys.stderr)
    print(
        f"节点 {len(graph.nodes)} 个，边 {len(graph.edges)} 条，"
        f"固定节点 {graph.fixed_node_count()} 个",
        file=sys.stderr,
    )
    try:
        result = optimize(graph, options=OptimizerOptions(verbose=args.verbose))
    except GraphNotSolvedError as exc:
        response = graph_error_to_dict(graph, [str(exc)], args.scenario)
        text = dump_response(response, args.output)
        if args.output is None:
            print(text)
        print(f"图结构诊断失败（符合预期演示）: {exc}", file=sys.stderr)
        return 1

    # 演示场景额外输出与真值的对比（合成数据才有真值）
    errors = []
    for nid in graph.ordered_ids:
        if graph.nodes[nid].fixed:
            continue
        est = result.poses[nid]
        ref = truth[nid]
        # 平移误差；固定了节点 0 的全局系，可直接比较
        xy_err = float(np.linalg.norm(est[:2] - ref[:2]))
        th_err = abs(wrap_angle(est[2] - ref[2]))
        errors.append((xy_err, th_err))
    if errors:
        max_xy = max(e[0] for e in errors)
        max_th = max(e[1] for e in errors)
        print(
            f"对真值最大误差: 平移 {max_xy:.4f} m，角度 {np.degrees(max_th):.3f}°",
            file=sys.stderr,
        )

    response = result_to_dict(result, graph, args.scenario)
    response["ground_truth_max_error"] = {
        "translation": max_xy if errors else 0.0,
        "theta_deg": float(np.degrees(max_th)) if errors else 0.0,
    }
    text = dump_response(response, args.output)
    if args.output is None:
        print(text)
    return 0 if result.success else 3


def _cmd_list_scenarios(_args: argparse.Namespace) -> int:
    for key, spec in standard_scenarios().items():
        print(f"{key:14s} {spec['description']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pose_graph_optimizer",
        description="二维 SE2 位姿图优化纯后端（NumPy 实现，JSON 离线入口）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_solve = sub.add_parser("solve", help="从 JSON 请求文件求解")
    p_solve.add_argument("-r", "--request", required=True, help="请求 JSON 路径")
    p_solve.add_argument("-o", "--output", help="响应 JSON 输出路径（默认打印到 stdout）")
    p_solve.add_argument("-v", "--verbose", action="store_true", help="打印迭代日志")
    p_solve.set_defaults(func=_cmd_solve)

    p_demo = sub.add_parser("demo", help="运行内置合成数据场景")
    p_demo.add_argument("-s", "--scenario", required=True, help="场景名")
    p_demo.add_argument("-o", "--output", help="响应 JSON 输出路径")
    p_demo.add_argument("--seed", type=int, default=42, help="随机种子")
    p_demo.add_argument("-v", "--verbose", action="store_true")
    p_demo.set_defaults(func=_cmd_demo)

    p_list = sub.add_parser("list-scenarios", help="列出内置场景")
    p_list.set_defaults(func=_cmd_list_scenarios)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
