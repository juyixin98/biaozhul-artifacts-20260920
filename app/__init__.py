"""collationsvc — 固定区域与 Unicode 版本的字符串排序和范围检索服务。

模块划分：
- config   配置与排序规则（规则指纹绑定索引版本）
- errors   失败类别与业务异常
- corpus   语料规范：载入、校验、身份保留
- kernel   挖掘内核：基于 PyICU 的排序键生成与稳定排序
- index    索引与模型：SQLite 持久化、版本管理、重建
- query    查询验证：游标、范围边界（排序键而非 UTF-8）
- api      FastAPI 接口、请求身份关联与可解释日志
"""

__version__ = "0.1.0"
