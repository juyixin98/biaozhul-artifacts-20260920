"""FastAPI 接口测试：用静态 Topology 注入假采集器，不需要 ROS 环境。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from qos_diag.models import Endpoint, QoSValues, Topology
from qos_diag.service import create_app


class FakeCollector:
    def __init__(self, topology: Topology):
        self.topology = topology

    def get_topology(self) -> Topology:
        return self.topology.model_copy(deep=True)


def make_topo() -> Topology:
    def ep(ep_type, node, gid, rel, dur, topic="/t", depth=10, state="discovered"):
        return Endpoint(
            topic=topic, endpoint_type=ep_type, node_name=node, endpoint_gid=gid,
            topic_type="std_msgs/msg/String",
            qos=QoSValues(reliability=rel, durability=dur, history="keep_last", depth=depth),
            first_seen=1.0, last_seen=2.0, state=state,
        )
    return Topology(
        captured_at=10.0, discovery_interval=1.0, missing_grace_seconds=5.0,
        endpoints=[
            ep("publisher", "pub_bad", "g1", "best_effort", "volatile"),
            ep("subscription", "sub_rel", "g2", "reliable", "volatile"),
            ep("publisher", "pub_ok", "g3", "reliable", "volatile", topic="/ok"),
            ep("subscription", "sub_ok", "g4", "reliable", "volatile", topic="/ok"),
        ],
        events=[],
    )


@pytest.fixture()
def client(tmp_path):
    app = create_app(collector=FakeCollector(make_topo()), data_dir=str(tmp_path))
    with TestClient(app) as c:
        yield c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["rules_version"].startswith("qos-rules-")


def test_rules_catalog_exposed(client):
    rules = client.get("/api/rules").json()["rules"]
    ids = {r["rule_id"] for r in rules}
    assert {"R-REL-001", "R-DUR-001", "R-HIST-001", "R-DEPTH-001"} <= ids


def test_topology_lists_endpoints(client):
    data = client.get("/api/topology").json()
    assert len(data["endpoints"]) == 4


def test_diagnose_flags_incompatible_topic(client):
    data = client.get("/api/diagnose").json()
    by_topic = {d["topic"]: d for d in data["topics"]}
    assert by_topic["/t"]["verdict"] == "incompatible"
    assert by_topic["/ok"]["verdict"] == "compatible"
    assert data["summary"]["incompatible"] == 1
    # 可解释链路存在
    findings = by_topic["/t"]["pairs"][0]["findings"]
    rel = next(f for f in findings if f["rule_id"] == "R-REL-001")
    assert rel["chain"]["publisher_value"] == "best_effort"
    assert rel["chain"]["subscription_value"] == "reliable"


def test_diagnose_unknown_topic_404(client):
    r = client.get("/api/diagnose?topic=/does/not/exist")
    assert r.status_code == 404
    assert "永久故障" in r.json()["detail"]


def test_snapshot_lifecycle_and_tamper(client, tmp_path):
    r = client.post("/api/snapshots")
    assert r.status_code == 201
    file_name = r.json()["file"]

    got = client.get(f"/api/snapshots/{file_name}")
    assert got.status_code == 200
    assert got.json()["rules_version"].startswith("qos-rules-")

    listing = client.get("/api/snapshots").json()["snapshots"]
    assert any(s["file"] == file_name for s in listing)

    # 篡改磁盘文件
    path = tmp_path / file_name
    raw = json.loads(path.read_text())
    raw["payload"] = raw["payload"].replace("best_effort", "reliableXXXXX", 1)
    path.write_text(json.dumps(raw))
    r = client.get(f"/api/snapshots/{file_name}")
    assert r.status_code == 409
