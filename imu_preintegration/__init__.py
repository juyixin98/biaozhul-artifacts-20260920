"""惯性数据预积分子集：已知重力与固定偏置下的 IMU 离线积分库。

范围说明：
- 仅做离线积分（姿态四元数更新 + 速度/位置积分），时间间隔可变。
- 不做完整 SLAM，不做偏置在线估计；偏置由请求方以固定值给出。
"""

from .integrator import ImuIntegrator, IntegrationConfig, IntegrationResult
from .validation import ValidationIssue, ValidationReport, validate_imu_stream

__all__ = [
    "ImuIntegrator",
    "IntegrationConfig",
    "IntegrationResult",
    "ValidationIssue",
    "ValidationReport",
    "validate_imu_stream",
]

__version__ = "0.1.0"
