"""Runnable ASGI entrypoint: ``uvicorn app.main:app``."""

from .api.app import create_app

app = create_app()
