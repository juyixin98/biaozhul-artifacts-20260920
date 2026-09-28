"""FastAPI application factory."""
from __future__ import annotations

from fastapi import FastAPI

from . import __version__
from .api.routes import router
from .config import Settings
from .logging_config import configure_logging


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    configure_logging(level=settings.log_level, log_path=str(settings.log_path))

    app = FastAPI(
        title="Restricted OWL Class-Expression Service",
        version=__version__,
        description=(
            "Backend reasoning service for a restricted OWL fragment: "
            "subclass, equivalence, disjointness and intersection only. "
            "Unsupported constructs are explicitly rejected."
        ),
    )
    app.state.settings = settings
    app.include_router(router)
    return app
