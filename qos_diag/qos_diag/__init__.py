"""QoS 兼容诊断服务（纯后端）。

模块划分：
- models:   数据模型（不依赖 rclpy）
- rules:    QoS 兼容规则引擎（不依赖 rclpy，可离线运行/单测）
- collector:基于 rclpy 的真实 DDS 端点发现与生命周期跟踪
- store:    拓扑快照持久化 + SHA-256/HMAC-SHA256 完整性保护（真实密码学运算）
- service:  FastAPI HTTP 服务
- cli:      命令行入口
"""

__version__ = "1.0.0"
