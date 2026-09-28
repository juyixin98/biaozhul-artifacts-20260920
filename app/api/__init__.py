"""FastAPI 服务层。"""

from app.api.app import create_app
from app.api.deps import AppState

__all__ = ["create_app", "AppState"]
