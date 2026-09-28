"""ASGI 入口：``uvicorn app.asgi:app``。

测试使用 :func:`app.api.app.create_app` 注入隔离配置，不在导入时产生
默认数据文件。
"""
from __future__ import annotations

from .api.app import create_app

app = create_app()
