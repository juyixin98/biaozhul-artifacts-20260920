"""本地知识抽取后端。

``flask --app app:create_app`` 可直接拿到应用工厂。
"""
from __future__ import annotations

__version__ = "1.0.0"

from .app import create_app  # noqa: F401  re-export 供 flask --app 使用
