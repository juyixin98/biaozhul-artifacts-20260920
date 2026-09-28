"""服务层：FastAPI 应用工厂与结构化运行日志。

默认应用实例采用懒加载：请用字符串目标 ``wfst.service.app:app`` 交给
uvicorn（访问属性时才构建），或调用 :func:`create_app` /
:func:`create_default_app`。本包不在 import 时产生建库/摄取副作用。
"""

from wfst.service.app import create_app, create_default_app, get_app
from wfst.service.logging import RunLogger, new_run_id

__all__ = [
    "create_app",
    "create_default_app",
    "get_app",
    "RunLogger",
    "new_run_id",
]
