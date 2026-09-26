"""JSON 请求/响应入口:从字典构造融合任务并导出结果。"""

from __future__ import annotations

from typing import Any

from occupancy_grid.fusion import SensorModel, SensorPose, integrate_scan
from occupancy_grid.grid import GridSpec, OccupancyGrid


def run_request(request: dict[str, Any]) -> dict[str, Any]:
    """执行一次融合请求,返回可 JSON 序列化的结果字典。

    请求格式见 README 与 examples/request.json。
    """
    grid_cfg = request["grid"]
    spec = GridSpec(
        width=int(grid_cfg["width"]),
        height=int(grid_cfg["height"]),
        resolution=float(grid_cfg["resolution"]),
        origin_x=float(grid_cfg.get("origin_x", 0.0)),
        origin_y=float(grid_cfg.get("origin_y", 0.0)),
    )
    grid = OccupancyGrid(
        spec,
        p_min=float(grid_cfg.get("p_min", 0.12)),
        p_max=float(grid_cfg.get("p_max", 0.97)),
    )

    model_cfg = request.get("sensor_model", {})
    model = SensorModel(
        p_occ=float(model_cfg.get("p_occ", 0.7)),
        p_free=float(model_cfg.get("p_free", 0.4)),
    )

    scans_out = []
    for scan in request.get("scans", []):
        pose_cfg = scan["pose"]
        pose = SensorPose(
            x=float(pose_cfg["x"]),
            y=float(pose_cfg["y"]),
            theta=float(pose_cfg.get("theta", 0.0)),
        )
        stats = integrate_scan(
            grid,
            pose,
            model,
            angles=[float(a) for a in scan["angles"]],
            ranges=[float(r) for r in scan["ranges"]],
            max_range=float(scan["max_range"]),
        )
        scans_out.append(
            {
                "ray_count": stats.ray_count,
                "hit_count": stats.hit_count,
                "miss_count": stats.miss_count,
                "out_of_bounds_endpoints": stats.out_of_bounds_endpoints,
                "free_cell_count": len(stats.free_cells),
                "occupied_cell_count": len(stats.occupied_cells),
            }
        )

    return {
        "grid": {
            "width": spec.width,
            "height": spec.height,
            "resolution": spec.resolution,
            "origin_x": spec.origin_x,
            "origin_y": spec.origin_y,
        },
        "scan_stats": scans_out,
        "log_odds": grid.log_odds.tolist(),
        "probability": grid.to_probability().tolist(),
    }
