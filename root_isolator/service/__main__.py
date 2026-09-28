"""Run the service with ``python -m root_isolator.service``."""

from __future__ import annotations

import uvicorn

from config.settings import load_settings
from root_isolator.service.app import create_app


def main() -> None:
    settings = load_settings()
    app = create_app(settings)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level=settings.log.level.lower())


if __name__ == "__main__":
    main()
