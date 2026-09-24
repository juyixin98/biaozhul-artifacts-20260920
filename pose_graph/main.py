"""Allow ``uvicorn pose_graph.main:app`` as an alternative entry point."""

from .app import app

__all__ = ["app"]
