"""本地启动入口: uvicorn app.main:app --reload"""

from .api import app

__all__ = ["app"]
