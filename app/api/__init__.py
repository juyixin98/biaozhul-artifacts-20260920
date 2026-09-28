"""FastAPI app factory."""

from __future__ import annotations

from fastapi import FastAPI

from .. import __version__
from ..config import Config
from ..logging_setup import configure_logging
from ..service import SolveService
from .routes import router


def create_app(config: Config | None = None) -> FastAPI:
    resolved = config or Config.load()
    configure_logging(resolved.log_level)
    app = FastAPI(
        title="Mixed-precision linear solver",
        version=__version__,
        description=(
            "Low-precision LU factorizations with high-precision residuals and "
            "iterative refinement; per-column backward-error evidence."
        ),
    )
    app.state.config = resolved
    app.state.solve_service = SolveService(resolved)
    app.include_router(router)
    return app


app = create_app()
