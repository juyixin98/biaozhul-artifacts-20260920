"""命令行 JSON 接口。

用法::

    cat request.json | python -m blp.cli
    python -m blp.cli request.json -o response.json
    python -m blp.cli request.json --rule dantzig

退出码：0 = optimal/unbounded/infeasible（正常给出结论）；
2 = 输入非法（响应 status=invalid_input）；
3 = 数值失败；4 = 达到迭代上限。
"""

from __future__ import annotations

import argparse
import json
import sys

from .errors import LPInputError, LPLimitError, LPNumericalError
from .jsonio import run_request

_EXIT_CODES = {
    "optimal": 0, "unbounded": 0, "infeasible": 0,
    "invalid_input": 2,
    "numeric_failure": 3,
    "iteration_limit": 4,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="blp",
        description="有界线性规划两阶段单纯形求解器（JSON 接口）",
    )
    parser.add_argument(
        "input", nargs="?",
        help="请求 JSON 文件；省略则从标准输入读取",
    )
    parser.add_argument("-o", "--output", help="响应写入文件；默认标准输出")
    parser.add_argument(
        "--rule", default="bland",
        choices=["bland", "dantzig", "largest_decrease", "auto"],
        help="枢轴规则（默认 bland，带防循环保证）",
    )
    parser.add_argument(
        "--indent", type=int, default=2, help="JSON 缩进（默认 2）"
    )
    args = parser.parse_args(argv)

    raw = open(args.input, encoding="utf-8").read() if args.input else \
        sys.stdin.read()
    try:
        req = json.loads(raw)
    except json.JSONDecodeError as exc:
        resp = {"status": "invalid_input",
                "detail": f"JSON 解析失败：{exc}"}
        code = 2
    else:
        try:
            resp = run_request(req, pivot_rule=args.rule)
            code = _EXIT_CODES.get(resp.get("status"), 1)
        except LPInputError as exc:
            resp = {"status": "invalid_input", "detail": str(exc)}
            code = 2
        except LPNumericalError as exc:
            resp = {"status": "numeric_failure", "detail": str(exc)}
            code = 3
        except LPLimitError as exc:
            resp = {"status": "iteration_limit", "detail": str(exc)}
            code = 4

    text = json.dumps(resp, ensure_ascii=False, indent=args_indent(args),
                      allow_nan=False)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    else:
        print(text)
    return code


def args_indent(args):
    return args.indent


if __name__ == "__main__":
    raise SystemExit(main())
