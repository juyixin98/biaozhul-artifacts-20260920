"""FastAPI application factory and process entrypoint."""

from __future__ import annotations

import os

from fastapi import FastAPI

from .api import build_router
from .evidence import EvidenceStore
from .logging_setup import configure_logging


def create_app(db_path: str | None = None) -> FastAPI:
    configure_logging()
    db_path = db_path or os.environ.get("PLANNER_DB", "data/evidence.db")
    store = EvidenceStore(db_path)
    app = FastAPI(
        title="STRIPS Offline Planning Service",
        version="1.0.0",
        description=(
            "Ground STRIPS planning with positive/negative preconditions "
            "and add/delete effects. Plans are independently re-executed; "
            "exhausted search bounds return verdict 'unknown'."
        ),
    )
    app.state.evidence_store = store
    app.include_router(build_router(store))
    return app


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run(
        "strips_planner.service:app",
        host=os.environ.get("PLANNER_HOST", "127.0.0.1"),
        port=int(os.environ.get("PLANNER_PORT", "8000")),
        reload=False,
    )


if __name__ == "__main__":
    main()
