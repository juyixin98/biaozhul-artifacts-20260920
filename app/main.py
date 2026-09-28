"""uvicorn entry point: ``uvicorn app.main:app``."""
from .api.app import app

__all__ = ["app"]
