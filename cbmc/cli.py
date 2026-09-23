"""命令行入口：python -m cbmc.cli check model.json [--steps N] [--timeout-ms N]
                  python -m cbmc.cli replay model.json '[{"action": "...", "params": {}}]'
"""

from __future__ import annotations

import argparse
import json
import sys

from .checker import BMC, MAX_BOUND, MAX_TIMEOUT_MS
from .errors import ModelError, ReplayError
from .invariants import default_invariant
from .model import validate_model
from .replay import replay_trace


def _load(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cbmc", description="合约有界模型检查器")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_check = sub.add_parser("check", help="有界模型检查")
    p_check.add_argument("model", help="合约模型 JSON 文件")
    p_check.add_argument("--steps", type=int, default=5)
    p_check.add_argument("--timeout-ms", type=int, default=10_000)
    p_check.add_argument("--custom-invariant", action="store_true",
                         help="使用模型自带 invariant（默认强制用内置非负+守恒不变量）")

    p_replay = sub.add_parser("replay", help="用具体解释器逐步重放动作序列")
    p_replay.add_argument("model")
    p_replay.add_argument("steps", help='动作序列 JSON，如 \'[{"action":"send","params":{"amount":1}}]\'')

    args = parser.parse_args(argv)
    try:
        model = validate_model(_load(args.model))
        if args.cmd == "check":
            if not args.custom_invariant or model.get("invariant") is None:
                model["invariant"] = default_invariant(model)
            result = BMC(model).check(args.steps, args.timeout_ms)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["status"] in ("counterexample", "no_counterexample") else 2
        else:
            inv = (model.get("invariant") or default_invariant(model))["expr"]
            steps = json.loads(args.steps)
            if not isinstance(steps, list):
                raise ModelError("steps 必须是数组")
            print(json.dumps({"trace": replay_trace(model, steps, inv)},
                             ensure_ascii=False, indent=2))
            return 0
    except (ModelError, ReplayError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
