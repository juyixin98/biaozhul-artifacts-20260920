"""异步 EKF 融合服务（纯后端）。

模块：
- config: 环境变量配置
- crypto: 真实 HMAC-SHA256 请求签名/验签
- schemas: 协议模型
- ekf: 二维位置/速度扩展卡尔曼滤波（稳定协方差更新）
- fusion: 按测量时间融合、2 秒迟到重放、检查点缓存、门限拒绝
- main: FastAPI 服务
"""

__version__ = "1.0.0"
