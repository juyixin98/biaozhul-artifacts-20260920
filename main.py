# -*- coding: utf-8 -*-
"""本地启动入口：python -m uvicorn app.api:app 或 python main.py"""
from __future__ import annotations

import uvicorn

if __name__ == "__main__":
    uvicorn.run("app.api:app", host="127.0.0.1", port=8000, reload=False)
