"""JSON 请求/响应入口：校验输入并调用里程计主流程。"""

from __future__ import annotations

from .config import RobotParams
from .pipeline import Pose, run_odometry

_REQUIRED_SAMPLE_KEYS = {"t", "left", "right"}


def _validate_request(request: dict) -> None:
    if not isinstance(request, dict):
        raise ValueError("请求必须是 JSON 对象")
    for key in ("robot", "samples"):
        if key not in request:
            raise ValueError(f"请求缺少必填字段: {key}")
    if not isinstance(request["samples"], list) or not request["samples"]:
        raise ValueError("samples 必须是非空数组")
    for i, sample in enumerate(request["samples"]):
        if not isinstance(sample, dict):
            raise ValueError(f"samples[{i}] 必须是对象")
        missing = _REQUIRED_SAMPLE_KEYS - set(sample)
        if missing:
            raise ValueError(f"samples[{i}] 缺少字段: {sorted(missing)}")


def handle_request(request: dict) -> dict:
    """处理一次里程计请求，返回可 JSON 序列化的响应。

    请求格式见 README 与 examples/。输入校验失败抛 ValueError。
    """
    _validate_request(request)

    params = RobotParams.from_dict(request["robot"])

    initial = request.get("initial_pose") or {}
    initial_pose = Pose(
        x_m=float(initial.get("x_m", 0.0)),
        y_m=float(initial.get("y_m", 0.0)),
        theta_rad=float(initial.get("theta_rad", 0.0)),
    )

    samples = request["samples"]
    timestamps = [float(s["t"]) for s in samples]
    left_counts = [int(s["left"]) for s in samples]
    right_counts = [int(s["right"]) for s in samples]

    result = run_odometry(
        params=params,
        timestamps=timestamps,
        left_counts=left_counts,
        right_counts=right_counts,
        initial_pose=initial_pose,
    )
    result["robot"] = request["robot"]
    return result
