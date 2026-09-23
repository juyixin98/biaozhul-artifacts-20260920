# -*- coding: utf-8 -*-
"""运行配置（环境变量）。"""
from __future__ import annotations

import os


def db_path() -> str:
    return os.environ.get("IBC_DB", os.path.join(os.getcwd(), "ibc_mini.db"))
