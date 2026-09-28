#!/usr/bin/env python3
"""Local entry point for the FastAPI query interface.

Usage:
    PYTHONPATH=src python3 scripts/run_api.py [--host 127.0.0.1] [--port 8000]

Environment overrides:
    HTN_DB_PATH       SQLite evidence database (default: data/planner.db)
    HTN_FIXTURE_DIR   fixture root (default: ./fixtures)
    HTN_LOG_LEVEL     DEBUG|INFO|WARNING (default: INFO)
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import uvicorn  # noqa: E402

from htn_planner.config import SETTINGS  # noqa: E402


def configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, SETTINGS.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the finite HTN planner API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    configure_logging()
    logging.getLogger("htn_planner").info(
        "starting service=%s version=%s db=%s fixtures=%s",
        SETTINGS.service_name, SETTINGS.service_version,
        SETTINGS.db_path, SETTINGS.fixture_dir,
    )
    uvicorn.run(
        "htn_planner.api:app",
        host=args.host, port=args.port,
        reload=False, log_level=SETTINGS.log_level.lower(),
    )


if __name__ == "__main__":
    main()
