"""ASGI entry point:  uvicorn adam_shards.server:app --config configs/default.json

The config path is read from the ADAM_SHARDS_CONFIG environment variable
(default ``configs/default.json``).
"""
from __future__ import annotations

import os

from .api import create_configured_app

CONFIG_PATH = os.environ.get("ADAM_SHARDS_CONFIG", "configs/default.json")
app = create_configured_app(CONFIG_PATH)
