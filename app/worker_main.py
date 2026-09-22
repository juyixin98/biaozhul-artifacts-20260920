"""独立工作器进程入口：python -m app.worker_main"""
from __future__ import annotations

import logging
import os

from .migrations import run_migrations
from .services.rule_packs import load_builtin_packs
from .services.worker import run_forever


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("KEX_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    run_migrations()
    load_builtin_packs()
    run_forever()


if __name__ == "__main__":
    main()
