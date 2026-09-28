"""Local service entrypoint: ``python -m app.main`` or ``uvicorn app.main:app``."""

from __future__ import annotations

import uvicorn

from .api import app
from .config import load_config


def main() -> None:
    config = load_config()
    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=8000,
        log_level=config.log_level.lower(),
    )


if __name__ == "__main__":
    main()
