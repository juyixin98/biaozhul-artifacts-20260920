"""``python -m twosls`` -> uvicorn entry point."""
from __future__ import annotations

import uvicorn

from .config import settings


def main() -> None:
    uvicorn.run(
        "twosls.api:app",
        host="127.0.0.1",
        port=8000,
        reload=False,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
