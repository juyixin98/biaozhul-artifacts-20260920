"""服务入口：python -m app.main"""

from __future__ import annotations

import uvicorn

from app.config import get_settings
from app.diagnostics import configure_logging


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    uvicorn.run(
        "app.service:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
