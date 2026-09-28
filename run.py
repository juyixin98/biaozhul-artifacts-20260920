"""可运行的服务入口。

用法::

    python run.py
    DAWG_PORT=8010 python run.py
"""

from __future__ import annotations

import uvicorn

from app.config import SETTINGS


def main() -> None:
    uvicorn.run(
        "app.api.app:app",
        host=SETTINGS.host,
        port=SETTINGS.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
