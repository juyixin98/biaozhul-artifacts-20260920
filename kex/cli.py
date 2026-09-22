"""Command-line entry points.

* ``python -m kex.cli worker``            — run a standalone worker process
* ``python -m kex.cli init-db``          — apply migrations
* ``python -m kex.cli seed-demo``        — create workspace + sample corpus
* ``python -m kex.cli list-workspaces``  — show workspace ids/names
"""
from __future__ import annotations

import json
import sys
import time

from .config import load_config
from .db import configure, session_scope
from .services.demo_corpus import SAMPLE_DOCUMENTS
from .services.jobs import Worker
from .services.workspaces import create_workspace


def cmd_worker() -> int:
    cfg = load_config()
    configure(cfg)
    worker = Worker(
        poll_interval=cfg.worker_poll_interval,
        lease_seconds=cfg.worker_lease_seconds,
        heartbeat_seconds=cfg.worker_heartbeat_seconds,
        max_workers=cfg.max_workers,
    )
    print(
        f"[worker] starting worker={worker.worker_id} max_workers={cfg.max_workers} "
        f"db={cfg.db_path}",
        flush=True,
    )
    try:
        worker.run_forever()
    except KeyboardInterrupt:
        print("[worker] shutting down", flush=True)
        from .db import session_scope as scope

        with scope() as db:
            from .services.jobs import deregister

            deregister(db, worker.worker_id)
    return 0


def cmd_init_db() -> int:
    import subprocess

    rc = subprocess.call(["alembic", "upgrade", "head"])
    return int(rc)


def cmd_seed_demo() -> int:
    cfg = load_config()
    configure(cfg)
    name = sys.argv[2] if len(sys.argv) > 2 else "demo"
    with session_scope() as db:
        from sqlalchemy import select

        from .models import Workspace

        existing = db.scalar(select(Workspace).where(Workspace.name == name))
        if existing is not None:
            print(json.dumps({
                "workspace_id": existing.id,
                "name": existing.name,
                "api_key": existing.api_key,
                "note": "workspace already existed",
            }, ensure_ascii=False, indent=2))
            return 0
        ws = create_workspace(db, name)
    api_key = ws.api_key

    # Upload documents through the service layer so jobs get enqueued.
    with session_scope() as db:
        from .services.documents import upload_document

        ids = []
        for title, text in SAMPLE_DOCUMENTS:
            ws_doc, dedup, job_id = upload_document(
                db, ws.id, text=text, title=title
            )
            ids.append({"document_id": ws_doc.id, "title": title, "job_id": job_id})

    print(
        json.dumps(
            {
                "workspace_id": ws.id,
                "name": name,
                "api_key": api_key,
                "documents": ids,
                "hint": "start a worker (`python -m kex.cli worker`) to process jobs",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_list_workspaces() -> int:
    cfg = load_config()
    configure(cfg)
    with session_scope() as db:
        from sqlalchemy import select

        from .models import Workspace

        rows = db.scalars(select(Workspace).order_by(Workspace.id)).all()
        for w in rows:
            print(f"{w.id}\t{w.name}")
    return 0


def cmd_wait_for_jobs(timeout: float = 60.0) -> int:
    """Helper used by the runnable sample: drain queues then exit."""
    cfg = load_config()
    configure(cfg)
    worker = Worker(
        poll_interval=0.2,
        lease_seconds=cfg.worker_lease_seconds,
        heartbeat_seconds=cfg.worker_heartbeat_seconds,
        max_workers=cfg.max_workers,
    )
    deadline = time.time() + timeout
    t = worker.start_in_thread()
    try:
        while time.time() < deadline:
            with session_scope() as db:
                from sqlalchemy import select

                from .models import JobItem

                pending = len(
                    db.execute(
                        select(JobItem.id).where(
                            JobItem.status.in_(["queued", "running"])
                        )
                    ).all()
                )
            if pending == 0:
                break
            time.sleep(0.3)
    finally:
        worker.stop()
        t.join(timeout=5)
    return 0


COMMANDS = {
    "worker": cmd_worker,
    "init-db": cmd_init_db,
    "seed-demo": cmd_seed_demo,
    "list-workspaces": cmd_list_workspaces,
    "wait": cmd_wait_for_jobs,
}


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv or argv[0] not in COMMANDS:
        print(f"usage: python -m kex.cli {{{'|'.join(COMMANDS)}}}", file=stderr())
        return 2
    return int(COMMANDS[argv[0]]())


def stderr():
    return sys.stderr


if __name__ == "__main__":
    raise SystemExit(main())
