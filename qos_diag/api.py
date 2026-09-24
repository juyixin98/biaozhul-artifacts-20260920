"""FastAPI application: REST surface for the QoS diagnostics service.

Endpoints
  GET  /health
  GET  /api/v1/topology                endpoint lifecycle view (timestamps/states)
  GET  /api/v1/diagnosis               every topic judged
  GET  /api/v1/diagnosis/{topic}       one topic judged, full explain chains
  POST /api/v1/snapshots               capture signed snapshot now
  GET  /api/v1/snapshots               list
  GET  /api/v1/snapshots/{id}          load
  GET  /api/v1/snapshots/{id}/verify   recompute content hash + HMAC + rules hash
  GET  /api/v1/rules/version           rules version + rules file hash

The app can boot in "sim" mode without rclpy (QOSDIAG_SIM=1) so the pure
logic and API are testable on machines without ROS; normal operation uses the
real rclpy monitor.
"""
from __future__ import annotations

import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query

from .qos_model import RULES_VERSION
from .service import DiagnosticsService
from .snapshot import SnapshotStore, rules_file_hash
from .topology import TopologyStore

def _data_dir() -> Path:
    return Path(os.environ.get(
        "QOSDIAG_DATA_DIR",
        Path(__file__).resolve().parent.parent / "data"))


GRACE_PERIOD_S = float(os.environ.get("QOSDIAG_GRACE_PERIOD_S", "10"))
POLL_PERIOD_S = float(os.environ.get("QOSDIAG_POLL_PERIOD_S", "0.5"))
SIM_MODE = os.environ.get("QOSDIAG_SIM", "0") == "1"

_state: dict = {}


def build_app(sim: bool | None = None, store: TopologyStore | None = None) -> FastAPI:
    sim = SIM_MODE if sim is None else sim

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        topo = store or TopologyStore(grace_period_s=GRACE_PERIOD_S)
        snaps = SnapshotStore(_data_dir() / "snapshots")
        svc = DiagnosticsService(topo, GRACE_PERIOD_S)
        _state.update(topo=topo, snaps=snaps, svc=svc, sim=sim,
                      started_ts=time.time())
        monitor_thread = None
        rclpy_mod = None
        if not sim:
            try:
                import rclpy
                from .nodes.monitor import MonitorNode
                rclpy.init()
                node = MonitorNode(topo, poll_period_s=POLL_PERIOD_S,
                                   grace_period_s=GRACE_PERIOD_S)
                monitor_thread = threading.Thread(
                    target=node.run, name="qosdiag-monitor", daemon=True)
                monitor_thread.start()
                _state.update(node=node, rclpy=rclpy)
            except ImportError:
                # rclpy unavailable: degrade, don't crash
                _state["sim"] = True
                _state["warning"] = "rclpy not importable; running in degraded sim mode"
        try:
            yield
        finally:
            node = _state.get("node")
            if node is not None:
                node.stop()
                node.destroy_node()
            if rclpy_mod is None and not sim:
                rclpy_mod = _state.get("rclpy")
            if rclpy_mod is not None and rclpy_mod.ok():
                rclpy_mod.shutdown()
            if monitor_thread is not None:
                monitor_thread.join(timeout=3)

    app = FastAPI(title="ROS2 QoS Compatibility Diagnostics",
                  version="1.0.0", lifespan=lifespan)

    def _need():
        if "svc" not in _state:
            raise HTTPException(503, "service still starting")
        return _state["topo"], _state["snaps"], _state["svc"]

    @app.get("/health")
    def health():
        return {
            "status": "ok",
            "mode": "sim" if _state.get("sim") else "ros2",
            "rmw": _safe_rmw_id(),
            "uptime_s": round(time.time() - _state.get("started_ts", time.time()), 3),
            "grace_period_s": GRACE_PERIOD_S,
            "warning": _state.get("warning"),
        }

    @app.get("/api/v1/rules/version")
    def rules_version():
        return {
            "rules_version": RULES_VERSION,
            "rules_file_sha256": rules_file_hash(),
            "rules": [
                {"id": "R1", "attribute": "reliability",
                 "effect": "INCOMPATIBLE when subscriber=RELIABLE publisher=BEST_EFFORT"},
                {"id": "R2", "attribute": "durability",
                 "effect": "INCOMPATIBLE when subscriber=TRANSIENT_LOCAL publisher=VOLATILE"},
                {"id": "R3", "attribute": "history",
                 "effect": "RISK when any side uses KEEP_ALL (unbounded buffer)"},
                {"id": "R4", "attribute": "depth",
                 "effect": "RISK when subscriber KEEP_LAST depth < min(10, publisher depth)"},
            ],
        }

    @app.get("/api/v1/topology")
    def topology(include_expired: bool = Query(True)):
        topo, _, _ = _need()
        now = time.time()
        eps = []
        for ep in topo.all():
            if not include_expired and ep.state.value == "EXPIRED":
                continue
            eps.append(_state["svc"]._endpoint_dto(ep, now))
        return {"generated_ts": now, "grace_period_s": GRACE_PERIOD_S,
                "endpoint_count": len(eps), "endpoints": eps}

    @app.get("/api/v1/diagnosis")
    def diagnosis_all():
        _, _, svc = _need()
        return svc.diagnose_all()

    @app.get("/api/v1/diagnosis/{topic:path}")
    def diagnosis_topic(topic: str):
        _, _, svc = _need()
        topic = "/" + topic.lstrip("/")
        res = svc.diagnose_topic(topic)
        if not res["publishers"] and not res["subscriptions"]:
            raise HTTPException(404, f"no endpoints discovered for {topic} yet "
                                     "(absence within grace window is not a failure)")
        return res

    @app.post("/api/v1/snapshots")
    def create_snapshot():
        _, snaps, svc = _need()
        body = {
            "topology": svc.diagnose_all(),
            "note": "captured from live graph",
        }
        rec = snaps.save(body)
        return {"snapshot_id": rec["snapshot_id"], "captured_ts": rec["captured_ts"],
                "rules_version": rec["rules_version"],
                "rules_hash": rec["rules_hash"],
                "content_sha256": rec["content_sha256"],
                "hmac_sha256": rec["hmac_sha256"]}

    @app.get("/api/v1/snapshots")
    def list_snapshots():
        _, snaps, _ = _need()
        return {"snapshots": snaps.list()}

    @app.get("/api/v1/snapshots/{snapshot_id}")
    def get_snapshot(snapshot_id: str):
        _, snaps, _ = _need()
        try:
            return snaps.load(snapshot_id)
        except FileNotFoundError:
            raise HTTPException(404, "snapshot not found")

    @app.get("/api/v1/snapshots/{snapshot_id}/verify")
    def verify_snapshot(snapshot_id: str):
        _, snaps, _ = _need()
        try:
            rec = snaps.load(snapshot_id)
        except FileNotFoundError:
            raise HTTPException(404, "snapshot not found")
        return {"snapshot_id": snapshot_id, "checks": snaps.verify(rec)}

    return app


def _safe_rmw_id() -> str | None:
    try:
        import rclpy
        if not rclpy.ok():
            rclpy.init()
        return rclpy.get_rmw_implementation_identifier()
    except Exception:
        return None


app = build_app()
