"""Run the FastAPI service with the pinned configuration.

Usage:
    .venv/bin/python -m scripts.run_server
"""
from __future__ import annotations

import uvicorn

from app.settings import load_settings


def main() -> None:
    settings = load_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.service.host,
        port=settings.service.port,
        log_level=settings.service.log_level,
        reload=False,
    )


if __name__ == "__main__":
    main()
