"""Local ASGI entry point.

Run with: uvicorn app.main:app --reload
"""

from __future__ import annotations

import os

from .api.app import create_app

log_dir = os.environ.get("RC_LOG_DIR", os.path.join(os.getcwd(), "logs"))
app = create_app(log_dir=log_dir)
