"""FastAPI application entry point."""
from __future__ import annotations

from fastapi import FastAPI

from . import config
from .api import router
from .database import init_db


def create_app() -> FastAPI:
    init_db()
    app = FastAPI(
        title="Battery-Reachable Task Allocation",
        version="1.0.0",
        description=(
            "Synthetic-robot task allocation with explicit energy formulas, "
            "charger reachability + safety margin, global minimum-cost "
            "matching, transactional battery/charger reservation, and "
            "charger-failure recomputation. **Planning demo only — no real "
            "control commands are emitted.**"
        ),
    )
    app.include_router(router)

    @app.get("/health", tags=["ops"])
    def health():
        return {
            "status": "ok",
            "service": "battery-reachable-task-allocation",
            "simulated": True,
            "safety_margin_fraction": config.SAFETY_MARGIN_FRACTION,
        }

    return app


app = create_app()
