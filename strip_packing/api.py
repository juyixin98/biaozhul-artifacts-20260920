"""JSON 命令行接口（纯后端，无任何前端组件）。

用法
----
::

    cat request.json | python3 -m strip_packing.api
    python3 -m strip_packing.api --input request.json [--output resp.json]

退出码
------
- 0  成功（含 ``ok_exact_limit_reached`` 等带信息的成功状态）；
- 1  实例被拒绝（输入范围/零尺寸/超宽等 :class:`PackingError`），
     stderr 与响应 JSON 中给出稳定错误码；
- 2  请求无法解析（非 JSON、顶层结构错误）。

请求样例见 ``examples/request_example.json``。
"""

from __future__ import annotations

import argparse
import json
import sys

from .validation import PackingError
from .solver import solve_packing


def process(raw_text: str) -> dict:
    """解析请求文本并求解，返回可 JSON 序列化的响应字典。"""
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as e:
        return _error("INVALID_JSON", f"请求不是合法 JSON: {e}")

    try:
        sol = solve_packing(payload)
    except PackingError as e:
        return _error(e.code, e.message)
    except (TypeError, KeyError) as e:
        return _error("INVALID_JSON", f"请求结构错误: {e}")

    return {
        "status": sol.status,
        "strip_width": sol.strip_width,
        "num_rectangles": sol.num_rectangles,
        "lower_bound": sol.lower_bound,
        "lower_bounds": {
            "area": sol.lower_bounds_detail["area"],
            "max_height": sol.lower_bounds_detail["max_height"],
            "pairwise": sol.lower_bounds_detail["pairwise"],
        },
        "heuristic_height": sol.heuristic_height,
        "heuristic_name": sol.heuristic_name,
        "optimality": "not_claimed",
        "gap_upper_minus_lower": sol.gap,
        "gap_ratio": sol.gap_ratio,
        "exact": sol.exact,
        "placements": sol.placements,
        "layout_verification": sol.verification,
        "note": ("heuristic_height 是最优高度的上界，不声明为全局最优；"
                 "lower_bound 是严格下界。exact.status=\"optimal\" 且 "
                 "gap=0 时二者夹逼成立，该布局被证明最优。"),
    }


def _error(code, message):
    return {"status": "error", "error_code": code, "error": message,
            "optimality": "not_claimed"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="二维固定宽条带装箱 JSON 接口（矩形不旋转）")
    parser.add_argument("--input", "-i", help="请求 JSON 文件；缺省读 stdin")
    parser.add_argument("--output", "-o", help="响应输出文件；缺省写 stdout")
    args = parser.parse_args(argv)

    if args.input:
        with open(args.input, "r", encoding="utf-8") as f:
            raw = f.read()
    else:
        raw = sys.stdin.read()

    resp = process(raw)
    text = json.dumps(resp, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    else:
        print(text)

    if resp["status"] == "error":
        return 1 if resp["error_code"] != "INVALID_JSON" else 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
