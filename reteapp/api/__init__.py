"""HTTP interface (FastAPI).

Endpoints
---------
``POST   /runs``                         create a run (compile rules)
``GET    /runs``                         list runs
``GET    /runs/{run_id}``                run header (version, settings)
``POST   /runs/{run_id}/facts``          insert a fact
``DELETE /runs/{run_id}/facts/{wme_id}`` retract a fact
``GET    /runs/{run_id}/facts``          working-memory listing
``GET    /runs/{run_id}/agenda``         conflict set in firing order + sources
``POST   /runs/{run_id}/fire``           fire next | step N | all (bounded)
``GET    /runs/{run_id}/network``        alpha/beta memory index digest
``GET    /runs/{run_id}/trace``          ordered structured decision trace
``GET    /runs/{run_id}/evidence``       SQLite evidence counters
``GET    /health`` / ``GET /version``

Every error response keeps the engine's explicit ``category``; nothing is
mapped to a generic 200 success.
"""

from .app import create_app

__all__ = ["create_app"]
