"""JSON 入口：读取融合请求 JSON，执行占据栅格融合，输出结果 JSON。

用法：
    python3 main.py --input examples/request_single_ray.json --output result.json
    cat request.json | python3 main.py          # 从 stdin 读，写到 stdout

请求格式见 README.md 与 examples/ 目录。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from occupancy_grid import GridParams, OccupancyGrid, Pose2D, integrate_scan


def _require(mapping: dict[str, Any], key: str, ctx: str) -> Any:
    if key not in mapping:
        raise ValueError(f"{ctx} 缺少必填字段 '{key}'")
    return mapping[key]


def run_request(request: dict[str, Any]) -> dict[str, Any]:
    """执行一次融合请求，返回可 JSON 序列化的结果。"""
    g = _require(request, "grid", "请求")
    p = request.get("params", {})

    params = GridParams(
        log_odds_occ=float(p.get("log_odds_occ", 0.85)),
        log_odds_free=float(p.get("log_odds_free", -0.4)),
        clamp_min=float(p.get("clamp_min", -4.0)),
        clamp_max=float(p.get("clamp_max", 4.0)),
    )
    grid = OccupancyGrid(
        width=int(_require(g, "width", "grid")),
        height=int(_require(g, "height", "grid")),
        resolution=float(_require(g, "resolution", "grid")),
        origin_x=float(g.get("origin", [0.0, 0.0])[0]),
        origin_y=float(g.get("origin", [0.0, 0.0])[1]),
        params=params,
    )

    scans = _require(request, "scans", "请求")
    if not isinstance(scans, list) or not scans:
        raise ValueError("scans 必须是非空数组")

    scan_stats = []
    for idx, scan in enumerate(scans):
        ctx = f"scans[{idx}]"
        pose_raw = _require(scan, "pose", ctx)
        pose = Pose2D(
            x=float(_require(pose_raw, "x", f"{ctx}.pose")),
            y=float(_require(pose_raw, "y", f"{ctx}.pose")),
            theta=float(_require(pose_raw, "theta", f"{ctx}.pose")),
        )
        stats = integrate_scan(
            grid,
            pose,
            ranges=[float(v) for v in _require(scan, "ranges", ctx)],
            angle_min=float(_require(scan, "angle_min", ctx)),
            angle_increment=float(_require(scan, "angle_increment", ctx)),
            range_max=float(_require(scan, "range_max", ctx)),
        )
        scan_stats.append(stats)

    prob = grid.probabilities()
    return {
        "grid": {
            "width": grid.width,
            "height": grid.height,
            "resolution": grid.resolution,
            "origin": [grid.origin_x, grid.origin_y],
        },
        "scan_stats": scan_stats,
        # 行优先嵌套列表：probabilities[row][col]，row 对应世界 y
        "probabilities": prob.round(6).tolist(),
        "log_odds": grid.log_odds.round(6).tolist(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="离散占据栅格融合（JSON 入口）")
    parser.add_argument("--input", help="请求 JSON 文件路径（缺省读 stdin）")
    parser.add_argument("--output", help="结果 JSON 输出路径（缺省写 stdout）")
    args = parser.parse_args(argv)

    try:
        if args.input:
            with open(args.input, encoding="utf-8") as f:
                request = json.load(f)
        else:
            request = json.load(sys.stdin)
        result = run_request(request)
    except (OSError, json.JSONDecodeError, ValueError, KeyError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1

    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
