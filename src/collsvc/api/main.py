"""Uvicorn entry point: ``python -m collsvc.api.main``."""
from __future__ import annotations

import uvicorn

from ..config import Settings
from .app import create_app

app = create_app()


def main() -> None:
    settings = Settings.from_env()
    uvicorn.run(
        "collsvc.api.main:app",
        host="127.0.0.1",
        port=8000,
        reload=False,
    )


if __name__ == "__main__":
    main()
