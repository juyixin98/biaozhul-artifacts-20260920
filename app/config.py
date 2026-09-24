"""运行期配置：求解参数（YAML）+ 密码学配置（环境变量）。"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .params_loader import PARAMS_PATH, load_params


@dataclass(frozen=True)
class SolverConfig:
    max_iterations: int
    position_tolerance: float
    orientation_tolerance: float
    damping_initial: float
    damping_max: float
    step_clip: float
    fd_eps: float
    verify_position_tolerance: float
    verify_orientation_tolerance: float
    singular_sigma_threshold: float
    singular_stall_iterations: int


@dataclass(frozen=True)
class CryptoConfig:
    """HMAC-SHA256 请求签名配置（真实密码学操作）。

    IK_API_KEY      共享密钥（必填，未配置时服务拒绝启动而非放行）
    IK_TIMESTAMP_TOLERANCE  时间戳容差（秒），默认 300，防重放
    IK_REQUIRE_AUTH 是否强制鉴权（默认 true；测试可经 env 关闭）
    """

    api_key: str
    timestamp_tolerance: float
    require_auth: bool


def load_solver_config() -> SolverConfig:
    s = load_params(PARAMS_PATH)["solver"]
    return SolverConfig(
        max_iterations=int(s["max_iterations"]),
        position_tolerance=float(s["position_tolerance"]),
        orientation_tolerance=float(s["orientation_tolerance"]),
        damping_initial=float(s["damping_initial"]),
        damping_max=float(s["damping_max"]),
        step_clip=float(s["step_clip"]),
        fd_eps=float(s["finite_difference_eps"]),
        verify_position_tolerance=float(s["verify_position_tolerance"]),
        verify_orientation_tolerance=float(s["verify_orientation_tolerance"]),
        singular_sigma_threshold=float(s["singular_sigma_threshold"]),
        singular_stall_iterations=int(s["singular_stall_iterations"]),
    )


def load_crypto_config() -> CryptoConfig:
    return CryptoConfig(
        api_key=os.environ.get("IK_API_KEY", ""),
        timestamp_tolerance=float(os.environ.get("IK_TIMESTAMP_TOLERANCE", "300")),
        require_auth=os.environ.get("IK_REQUIRE_AUTH", "true").lower() not in ("0", "false", "no"),
    )
