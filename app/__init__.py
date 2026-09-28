"""不可变有序词典的最小无环确定自动机 (MADFA / DAWG) 服务。

分层组织::

    app.corpus  语料规范：词项校验、有序性保证、合成夹具
    app.core    挖掘内核：状态模型、增量最小化 DAWG、参考 Trie
    app.index   索引与模型：SQLite 持久化与引用完整性校验
    app.query   查询验证：成员判定与前缀计数
    app.api     FastAPI 服务入口
"""

__version__ = "1.0.0"
