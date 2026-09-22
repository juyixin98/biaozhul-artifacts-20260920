"""Flask 应用工厂、错误处理与 CLI。"""
from __future__ import annotations

import logging
import os
import threading

from flask import Flask, jsonify

from .config import config
from .rules_engine import RulePackError
from .services import documents as docs_service
from .services import workspaces as ws_service
from .services.indexing import RebuildInProgress as IndexRebuild
from .services.jobs import JobNotFound, RebuildInProgress as JobRebuild
from .services.rebuild import ActivationError

log = logging.getLogger("kex")


def create_app(*, start_workers: bool | None = None) -> Flask:
    app = Flask(__name__)
    app.url_map.strict_slashes = False

    logging.basicConfig(
        level=os.environ.get("KEX_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    from .api import admin_bp, api_bp

    app.register_blueprint(api_bp)
    app.register_blueprint(admin_bp)

    @app.get("/healthz")
    def healthz():
        return jsonify({"status": "ok"})

    @app.errorhandler(ws_service.AuthError)
    def _auth_err(exc):
        return jsonify({"error": "unauthorized", "message": str(exc)}), 401

    @app.errorhandler(FileNotFoundError)
    def _not_found(exc):
        return jsonify({"error": "not_found", "message": str(exc)}), 404

    @app.errorhandler(ValueError)
    def _bad_request(exc):
        return jsonify({"error": "bad_request", "message": str(exc)}), 400

    @app.errorhandler(RulePackError)
    def _pack_err(exc):
        return jsonify({"error": "invalid_rule_pack", "message": str(exc)}), 400

    @app.errorhandler(ActivationError)
    def _activation_err(exc):
        msg = str(exc)
        code = 409 if ("重建" in msg or "building" in msg) else 400
        return jsonify({"error": "activation_conflict", "message": msg}), code

    @app.errorhandler(JobRebuild)
    @app.errorhandler(IndexRebuild)
    def _rebuild_busy(exc):
        return jsonify({"error": "rebuild_in_progress", "message": str(exc)}), 409

    @app.errorhandler(JobNotFound)
    def _job_missing(exc):
        return jsonify({"error": "not_found", "message": f"作业 {exc} 不存在"}), 404

    @app.errorhandler(KeyError)
    def _key_err(exc):
        return jsonify({"error": "not_found", "message": str(exc)}), 404

    @app.errorhandler(docs_service.WorkspaceError)
    def _ws_err(exc):
        return jsonify({"error": "workspace_error", "message": str(exc)}), 400

    @app.cli.command("init-db")
    def init_db_cmd():
        """迁移并载入内置规则。"""
        from .migrations import run_migrations
        from .services.rule_packs import load_builtin_packs

        applied = run_migrations()
        log.info("migrations applied: %s", applied or "none (up to date)")
        for compiled, created in load_builtin_packs():
            log.info(
                "rule pack %s: %s",
                compiled.version,
                "published" if created else "already present",
            )

    @app.cli.command("run-worker")
    def run_worker_cmd():
        """独立工作器进程（租约上限仍由 KEX_MAX_WORKERS 全局约束）。"""
        from .services.worker import run_forever

        run_forever()

    should_start = config.enable_worker_threads if start_workers is None else start_workers
    if should_start and os.environ.get("KEX_ROLE", "api") == "api":
        # 延迟启动，避免 import 应用时就连数据库；线程数受全局租约上限保护。
        def _start():
            from .migrations import run_migrations
            from .services.rule_packs import load_builtin_packs
            from .services.worker import Worker

            run_migrations()
            load_builtin_packs()
            n = max(1, min(config.max_workers, 2))
            app.extensions["kex_workers"] = [
                Worker(poll_interval=0.2) for _ in range(n)
            ]
            for w in app.extensions["kex_workers"]:
                w.start()

        threading.Thread(target=_start, name="kex-worker-bootstrap", daemon=True).start()

    return app
