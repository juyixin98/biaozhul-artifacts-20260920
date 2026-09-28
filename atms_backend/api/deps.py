"""Dependency wiring: repository, service and request ids."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import Header

from ..core.budgets import Budgets
from ..diagnostics import new_request_id
from ..services.engine_service import ATMService
from ..storage.repository import Repository
from ..storage.schema import connect, initialize

logger = logging.getLogger("atms")


def init_service(db_path: str, budgets: Budgets) -> ATMService:
    conn = connect(db_path)
    initialize(conn)
    return ATMService(Repository(conn), budgets)


def request_id_dep(
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-ID"),
) -> str:
    if x_request_id and len(x_request_id) <= 120:
        return x_request_id
    return new_request_id()
