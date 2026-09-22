"""Flask application factory.

Serves the JSON API and optionally hosts one embedded background worker
thread (``KEX_EMBED_WORKER=1``) so the single-container Docker demo works
without a separate worker process.
"""
from __future__ import annotations

from flask import Flask, jsonify

from .config import Config, load_config
from .db import configure as configure_db
from .db import get_engine
from .web.api import api_bp
from .web.auth import WorkspaceError


def create_app(config: Config | None = None, *, start_worker: bool | None = None) -> Flask:
    cfg = config or load_config()
    configure_db(cfg)

    app = Flask(__name__)
    app.url_map.strict_slashes = False
    app.register_blueprint(api_bp)

    @app.errorhandler(WorkspaceError)
    def _handle_service_error(exc: WorkspaceError):  # noqa: ANN202
        return jsonify({"error": str(exc)}), exc.status

    @app.errorhandler(404)
    def _handle_404(_exc):  # noqa: ANN202, ANN001
        return jsonify({"error": "not found"}), 404

    @app.errorhandler(405)
    def _handle_405(_exc):  # noqa: ANN202, ANN001
        return jsonify({"error": "method not allowed"}), 405

    @app.get("/health")
    def health():  # noqa: ANN202
        # Verify DB connectivity on every health probe.
        from sqlalchemy import text

        engine = get_engine()
        with engine.connect() as conn:
            (journal,) = conn.execute(text("PRAGMA journal_mode")).fetchone()
        return jsonify({"status": "ok", "journal_mode": journal})

    # Embedded worker (single-container demo). Real deployments run a
    # standalone worker container instead (or additionally, cap permitting).
    should_start = cfg.embedded_worker if start_worker is None else start_worker
    if should_start:
        from .worker_runner import start_embedded_worker

        start_embedded_worker(cfg, app)

    return app
