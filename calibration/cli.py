"""命令行入口。

用法::

    # 从 JSON 文件读取请求
    python -m calibration.cli evaluate examples/request_basic.json

    # 从标准输入读取
    cat examples/request_basic.json | python -m calibration.cli evaluate -

    # 运行内置可复现合成数据演示
    python -m calibration.cli demo --n-samples 1000 --seed 42
"""

from __future__ import annotations

import argparse
import json
import sys

from .errors import CalibrationError
from .metrics import expected_calibration_error
from .service import CalibrationService
from .synthetic import make_demo_dataset


def _load_request(path: str) -> dict:
    if path == "-":
        raw = sys.stdin.read()
    else:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    try:
        request = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CalibrationError(
            "INVALID_JSON", f"请求不是合法 JSON: {exc.msg}（第 {exc.lineno} 行）"
        ) from exc
    return request


def _cmd_evaluate(args) -> int:
    service = CalibrationService()
    response = service.evaluate(_load_request(args.file))
    json.dump(response, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0 if response["success"] else 1


def _cmd_demo(args) -> int:
    data = make_demo_dataset(
        n_samples=args.n_samples,
        seed=args.seed,
        prior=args.prior,
        temperature=args.temperature,
    )
    request = {"y_true": data["y"].tolist(), "proba": data["proba"].tolist()}
    response = CalibrationService().evaluate(request)
    if response["success"]:
        response["data"]["demo_meta"] = {
            "n_samples": args.n_samples,
            "seed": args.seed,
            "prior": args.prior,
            "temperature": args.temperature,
            "observed_positive_rate": data["positive_prevalence"],
            "ece_of_true_probability": expected_calibration_error(
                data["y"], data["p_true"], n_bins=args.n_bins
            ),
        }
    json.dump(response, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0 if response["success"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="calibration",
        description="二分类概率校准误差评估服务（纯后端，NumPy 实现）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_eval = sub.add_parser("evaluate", help="从 JSON 文件（或 '-' 表示 stdin）读取评估请求")
    p_eval.add_argument("file", help="请求 JSON 路径，'-' 表示从标准输入读取")
    p_eval.set_defaults(func=_cmd_evaluate)

    p_demo = sub.add_parser("demo", help="用可复现的合成数据运行完整评估演示")
    p_demo.add_argument("--n-samples", type=int, default=1000)
    p_demo.add_argument("--seed", type=int, default=42)
    p_demo.add_argument("--prior", type=float, default=0.3)
    p_demo.add_argument("--temperature", type=float, default=2.0)
    p_demo.add_argument("--n-bins", type=int, default=10)
    p_demo.set_defaults(func=_cmd_demo)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except CalibrationError as exc:
        json.dump(
            {"success": False, "data": None, "error": exc.to_dict()},
            sys.stdout,
            ensure_ascii=False,
            indent=2,
        )
        sys.stdout.write("\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
