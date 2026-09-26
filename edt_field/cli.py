"""JSON 入口：从文件读取请求，计算距离场，输出 JSON 响应。

用法:
    python -m edt_field 请求.json [-o 响应.json]

请求格式:
    {
      "grid": [[0, 1, 0], [0, 0, 0]],   // 必填，矩形二维数组，0=自由 1=障碍
      "cell_size": [0.5, 1.0]           // 可选，[行尺寸dy, 列尺寸dx]，默认 [1, 1]
    }

响应格式:
    {
      "shape": [行数, 列数],
      "cell_size": [dy, dx],
      "distances": [[...]],   // 每格最近障碍欧氏距离；全空地图为 null
      "nearest": [[[行, 列], ...]]  // 每格最近障碍来源下标；全空地图为 null
    }
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from typing import Any

import numpy as np

from edt_field.core import euclidean_distance_transform


class RequestError(ValueError):
    """请求内容不合法。"""


def _validate_grid(raw: Any) -> np.ndarray:
    if not isinstance(raw, list) or not raw:
        raise RequestError("grid 必须是非空二维数组")
    if not all(isinstance(row, list) and row for row in raw):
        raise RequestError("grid 的每一行必须是非空数组")
    width = len(raw[0])
    if any(len(row) != width for row in raw):
        raise RequestError("grid 必须是矩形：各行长度不一致")
    grid = np.array(raw)
    if not np.isin(grid, [0, 1, False, True]).all():
        raise RequestError("grid 只能包含 0/1（或 false/true）")
    return grid.astype(bool)


def _validate_cell_size(raw: Any) -> tuple[float, float]:
    if raw is None:
        return (1.0, 1.0)
    if isinstance(raw, (int, float)):
        raw = [raw, raw]
    if (
        not isinstance(raw, list)
        or len(raw) != 2
        or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in raw)
    ):
        raise RequestError("cell_size 必须是正数或 [dy, dx] 两个正数")
    dy, dx = float(raw[0]), float(raw[1])
    if not (math.isfinite(dy) and dy > 0 and math.isfinite(dx) and dx > 0):
        raise RequestError("cell_size 必须为正有限数")
    return (dy, dx)


def compute_response(request: dict[str, Any]) -> dict[str, Any]:
    """校验请求并计算距离场，返回可 JSON 序列化的响应字典。"""
    if not isinstance(request, dict):
        raise RequestError("请求必须是 JSON 对象")
    if "grid" not in request:
        raise RequestError("请求缺少必填字段 grid")
    grid = _validate_grid(request["grid"])
    cell_size = _validate_cell_size(request.get("cell_size"))

    field = euclidean_distance_transform(grid, cell_size)
    rows, cols = grid.shape

    distances: list[list[Any]] = []
    nearest: list[list[Any]] = []
    for r in range(rows):
        dist_row: list[Any] = []
        near_row: list[Any] = []
        for c in range(cols):
            value = float(field.distances[r, c])
            if math.isinf(value):
                dist_row.append(None)  # JSON 无法表示 inf，用 null
                near_row.append(None)
            else:
                dist_row.append(value)
                near_row.append(
                    [int(field.source_rows[r, c]), int(field.source_cols[r, c])]
                )
        distances.append(dist_row)
        nearest.append(near_row)

    return {
        "shape": [rows, cols],
        "cell_size": [cell_size[0], cell_size[1]],
        "distances": distances,
        "nearest": nearest,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="edt_field", description="二维栅格最近障碍距离场（精确欧氏距离变换）"
    )
    parser.add_argument("request", help="请求 JSON 文件路径，'-' 表示标准输入")
    parser.add_argument("-o", "--output", help="响应输出路径，缺省输出到标准输出")
    args = parser.parse_args(argv)

    try:
        if args.request == "-":
            request = json.load(sys.stdin)
        else:
            with open(args.request, encoding="utf-8") as fh:
                request = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"读取请求失败: {exc}", file=sys.stderr)
        return 2

    try:
        response = compute_response(request)
    except RequestError as exc:
        print(f"请求不合法: {exc}", file=sys.stderr)
        return 2

    text = json.dumps(response, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
