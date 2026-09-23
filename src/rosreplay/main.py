"""Service entry point: ``python -m rosreplay.main`` or the ``ros-replay`` script."""
from __future__ import annotations

import argparse

import uvicorn

from .api import create_app
from .config import get_settings


def main() -> None:
    parser = argparse.ArgumentParser(description="ROS replay checkpoint service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    settings = get_settings()
    settings.ensure_dirs()
    app = create_app(settings)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
