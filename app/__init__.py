"""分层区组随机分配服务（stratified permuted-block randomization）。

模块职责：
- ``app.contracts``      统计契约：臂、比例、分层因子、区组与尾组策略
- ``app.core``           估计内核：身份冻结、随机流、区组置换、分配编排
- ``app.storage``        SQLite 事务仓储（幂等、并发声明、审计落盘）
- ``app.api``            FastAPI 路由、请求/响应模型、权限隔离
- ``app.evidence``       证据与诊断：均衡核算、流复核、失败与不确定结论
- ``app.reproducibility``复现实验：固定夹具双跑、来源记录、JSON 报告
"""

__version__ = "1.0.0"
SCHEMA_VERSION = 1
RANDOM_STREAM_DOMAIN = "rct/v1"
