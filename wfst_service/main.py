"""Local server entrypoint: ``python -m wfst_service.main``."""

from __future__ import annotations

import uvicorn

from .config import SETTINGS
from .logging_setup import configure_logging


def main() -> None:
    configure_logging(SETTINGS.log_level)
    uvicorn.run(
        "wfst_service.api.app:create_app",
        factory=True,
        host=SETTINGS.host,
        port=SETTINGS.port,
        log_level=SETTINGS.log_level.lower(),
    )


if __name__ == "__main__":
    main()
