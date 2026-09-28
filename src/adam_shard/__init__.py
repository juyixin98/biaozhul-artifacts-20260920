"""Local multi-process Adam state sharding with cross-world-size recovery."""

from __future__ import annotations

import logging

__version__ = "1.0.0"

logging.getLogger("adam_shard").addHandler(logging.NullHandler())
