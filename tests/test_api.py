"""API tests in sim mode with a seeded TopologyStore (no ROS needed)."""
import time

import pytest
from fastapi.testclient import TestClient

from qos_diag.api import build_app
from qos_diag.topology import TopologyStore


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("QOSDIAG_DATA_DIR", str(tmp_path))
    store = TopologyStore(grace_period_s=10)
    now = time.time()
    # R1 incompatible pair
    store.upsert_discovered(
        gid="gidpub1", topic="/data", role="publisher", node="pub_be", ns="/",
        message_type="std_msgs/msg/String",
        rmw_reliability="BEST_EFFORT", rmw_durability="VOLATILE",
        rmw_history="UNKNOWN", rmw_depth=0, ts=now)
    store.upsert_discovered(
        gid="gidsub1", topic="/data", role="subscription", node="sub_rel", ns="/",
        message_type="std_msgs/msg/String",
        rmw_reliability="RELIABLE", rmw_durability="VOLATILE",
        rmw_history="UNKNOWN", rmw_depth=0, ts=now)
    from qos_diag.topology import Announcement
    store.apply_announcement(Announcement(
        node_name="pub_be", role="publisher", topic="/data",
        reliability="BEST_EFFORT", durability="VOLATILE",
        history="KEEP_LAST", depth=10, seq=1, sent_ts=now), now)
    store.apply_announcement(Announcement(
        node_name="sub_rel", role="subscription", topic="/data",
        reliability="RELIABLE", durability="VOLATILE",
        history="KEEP_LAST", depth=10, seq=1, sent_ts=now), now)
    app = build_app(sim=True, store=store)
    with TestClient(app) as c:
        yield c


def test_health_sim(client):
    r = client.get("/health")
    assert r.json()["status"] == "ok"
    assert r.json()["mode"] == "sim"


def test_rules_version_endpoint(client):
    r = client.get("/api/v1/rules/version")
    assert r.json()["rules_version"]
    assert {x["id"] for x in r.json()["rules"]} == {"R1", "R2", "R3", "R4"}


def test_diagnosis_flags_r1(client):
    r = client.get("/api/v1/diagnosis/data")
    assert r.status_code == 200
    data = r.json()
    assert data["severity"] == "INCOMPATIBLE"
    m = data["matches"][0]
    assert m["blocking_rules"] == ["R1"]
    # explainable chain complete
    assert [s["rule_id"] for s in m["steps"]] == ["R1", "R2", "R3", "R4"]


def test_topology_includes_timestamps_and_provenance(client):
    data = client.get("/api/v1/topology").json()
    assert data["endpoint_count"] == 2
    pub = next(e for e in data["endpoints"] if e["node"] == "/pub_be")
    assert pub["first_seen_ts"] and pub["last_seen_ts"]
    assert pub["qos"]["depth"] == 10
    assert pub["provenance"]["depth"] == "endpoint_announce"
    assert pub["provenance"]["reliability"] == "rmw_discovery"


def test_snapshot_lifecycle_and_verify(client):
    sid = client.post("/api/v1/snapshots").json()["snapshot_id"]
    ids = {s["snapshot_id"] for s in client.get("/api/v1/snapshots").json()["snapshots"]}
    assert sid in ids
    rec = client.get(f"/api/v1/snapshots/{sid}").json()
    assert rec["rules_version"]
    v = client.get(f"/api/v1/snapshots/{sid}/verify").json()
    assert v["checks"]["all_ok"] is True


def test_unknown_topic_is_404_not_permanent_failure(client):
    assert client.get("/api/v1/diagnosis/never_seen").status_code == 404
