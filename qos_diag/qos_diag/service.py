"""FastAPI HTTP 服务：暴露实时拓扑、QoS 诊断、端点事件与受完整性保护的快照。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.concurrency import run_in_threadpool

from .collector import QoSTopologyCollector, require_rclpy
from .models import Topology
from .rules import RULES_VERSION, diagnose_topology, list_rules
from .store import SnapshotStore, canonical_payload


def create_app(
    collector: QoSTopologyCollector | None = None,
    data_dir: str | os.PathLike[str] | None = None,
) -> FastAPI:
    """构造 FastAPI app。

    collector 可注入（测试场景）；默认为 None 时延迟初始化，由 CLI 负责启动。
    """
    app = FastAPI(
        title="QoS 兼容诊断服务",
        version="1.0.0",
        description="采集 ROS 2 真实发布/订阅端点，诊断 reliability/durability/history/depth "
        "的确定不兼容与性能风险，提供带时间戳的端点生命周期与可解释匹配链路。",
    )
    app.state.collector = collector
    app.state.store = SnapshotStore(data_dir or os.environ.get("QOSDIAG_DATA", "data"))

    def _collector() -> QoSTopologyCollector:
        if app.state.collector is None:
            require_rclpy()
            app.state.collector = QoSTopologyCollector()
            app.state.collector.start()
        return app.state.collector

    async def _topology(include_lost: bool) -> Topology:
        topo = await run_in_threadpool(_collector().get_topology)
        if not include_lost:
            topo = topo.model_copy(
                update={"endpoints": [e for e in topo.endpoints if e.state != "lost"]}
            )
        return topo

    @app.get("/health", tags=["meta"])
    async def health() -> dict[str, str]:
        return {"status": "ok", "rules_version": RULES_VERSION}

    @app.get("/api/rules", tags=["diagnosis"])
    async def get_rules() -> dict[str, Any]:
        return {"rules_version": RULES_VERSION, "rules": list_rules()}

    @app.get("/api/topology", tags=["topology"])
    async def get_topology(
        include_lost: bool = Query(False, description="包含已确认消失(lost)的端点"),
        include_system: bool = Query(True, description="包含 /rosout、/parameter_events 等系统话题"),
    ) -> dict[str, Any]:
        topo = await _topology(include_lost)
        if not include_system:
            sys_topics = {"/rosout", "/parameter_events"}
            topo = topo.model_copy(
                update={
                    "endpoints": [e for e in topo.endpoints if e.topic not in sys_topics],
                    "events": [ev for ev in topo.events if ev.topic not in sys_topics],
                }
            )
        return topo.model_dump()

    @app.get("/api/events", tags=["topology"])
    async def get_events(
        limit: int = Query(100, ge=1, le=2000),
        topic: str | None = Query(None),
    ) -> dict[str, Any]:
        topo = await _topology(include_lost=True)
        events = list(topo.events)
        if topic:
            events = [e for e in events if e.topic == topic]
        return {"count": len(events[-limit:]), "events": [e.model_dump() for e in events[-limit:]]}

    @app.get("/api/diagnose", tags=["diagnosis"])
    async def diagnose(
        topic: str | None = Query(None, description="只诊断指定话题"),
        include_system: bool = Query(False),
    ) -> dict[str, Any]:
        topo = await _topology(include_lost=False)
        if not include_system:
            sys_topics = {"/rosout", "/parameter_events"}
            topo = topo.model_copy(
                update={"endpoints": [e for e in topo.endpoints if e.topic not in sys_topics]}
            )
        diagnoses = diagnose_topology(topo)
        if topic is not None:
            diagnoses = [d for d in diagnoses if d.topic == topic]
            if not diagnoses:
                raise HTTPException(
                    status_code=404,
                    detail=f"话题 {topic} 当前没有已发现端点（端点未发现不等于永久故障，请稍后重试）",
                )
        summary: dict[str, int] = {}
        for d in diagnoses:
            summary[d.verdict] = summary.get(d.verdict, 0) + 1
        return {
            "rules_version": RULES_VERSION,
            "captured_at": topo.captured_at,
            "summary": summary,
            "topics": [d.model_dump() for d in diagnoses],
        }

    @app.post("/api/snapshots", status_code=201, tags=["snapshots"])
    async def create_snapshot() -> dict[str, Any]:
        topo = await _topology(include_lost=True)
        snapshot = app.state.store.create_snapshot(topo)
        path = await run_in_threadpool(app.state.store.save, snapshot)
        return {
            "snapshot_id": snapshot.snapshot_id,
            "file": os.path.basename(path),
            "rules_version": snapshot.rules_version,
            "sha256": app.state.store.sha256(canonical_payload(snapshot)),
        }

    @app.get("/api/snapshots", tags=["snapshots"])
    async def list_snapshots() -> dict[str, Any]:
        return {"snapshots": app.state.store.list_snapshots()}

    @app.get("/api/snapshots/{file_name}", tags=["snapshots"])
    async def get_snapshot(file_name: str) -> dict[str, Any]:
        try:
            snapshot = await run_in_threadpool(app.state.store.load, file_name)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="快照不存在")
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return snapshot.model_dump()

    return app


app = create_app()
