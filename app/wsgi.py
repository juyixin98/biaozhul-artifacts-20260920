"""Waitress WSGI 入口：python -m app.wsgi

迁移必须在任何工作器线程启动前完成，因此这里先迁移再构造 app。
"""
from __future__ import annotations

from waitress import serve

from .app import create_app
from .config import config
from .migrations import run_migrations
from .services.rule_packs import load_builtin_packs


def main() -> None:
    run_migrations()
    load_builtin_packs()
    app = create_app()
    print(
        f"* KEX listening on {config.http_host}:{config.http_port} "
        f"(WAL SQLite at {config.db_url})"
    )
    serve(app, host=config.http_host, port=config.http_port, threads=8)


if __name__ == "__main__":
    main()
