"""小型加权有限状态转换器（WFST）服务。

模块分层：
- core      : FST 数据结构、epsilon 语义、环检测
- algorithms: 组合（epsilon 过滤）、k-最短路径、代价算子
- corpus    : 语料规范与合成夹具
- mining    : 从对齐语料挖掘词典/规则片段
- index     : SQLite 持久化与模型装载
- service    : FastAPI HTTP 层
- config    : 配置
"""

__version__ = "1.0.0"
