#!/usr/bin/env python3
"""End-to-end acceptance against the REAL service and REAL ROS 2 processes.

Stages (unique topic per run so reruns never mix with stale state):

  Phase A reliability:
    publisher BEST_EFFORT + subscriber RELIABLE        -> INCOMPATIBLE (R1)
    restart subscriber as BEST_EFFORT                  -> COMPATIBLE
  Phase B durability:
    publisher VOLATILE + subscriber TRANSIENT_LOCAL    -> INCOMPATIBLE (R2)
    restart publisher as TRANSIENT_LOCAL               -> COMPATIBLE (RISK R3 if KEEP_ALL)
  Snapshot: capture; content hash + HMAC + rules hash verify;
            tamper body on disk -> content/HMAC checks FAIL (real crypto).
  Grace:    SIGKILL a publisher (no DDS goodbye): within grace -> SUSPECT
            (never reported as permanent failure), after grace -> EXPIRED;
            a new publisher with the same node identity -> ACTIVE (returns=1).

Exit 0 only if every assertion holds. Requires ROS sourced.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
PORT = os.environ.get("QOSDIAG_PORT", "8077")
BASE = os.environ.get("QOSDIAG_BASE_URL", f"http://127.0.0.1:{PORT}")
GRACE = float(os.environ.get("QOSDIAG_GRACE_PERIOD_S", "3"))
# Fast-DDS default participant lease is 20s: after SIGKILL (no DDS goodbye)
# the endpoint stays in the graph until the lease lapses. This itself is part
# of the demonstration — absence is not known instantly.
DDS_LEASE_WAIT_S = float(os.environ.get("QOSDIAG_DDS_LEASE_WAIT_S", "28"))
RUN_TAG = format(int(time.time()) * 1000 + os.getpid() % 1000, "x")
TOPIC_A = f"/qos_acc/rel/run{RUN_TAG}"
TOPIC_B = f"/qos_acc/dur/run{RUN_TAG}"
ENV = {**os.environ,
       "QOSDIAG_GRACE_PERIOD_S": str(GRACE),
       "QOSDIAG_DATA_DIR": str(ROOT / "data" / f"acc-{RUN_TAG}")}


def log(msg):
    print(f"[acc {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def wait_health(timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            r = requests.get(f"{BASE}/health", timeout=2)
            if r.ok and r.json()["status"] == "ok":
                return r.json()
        except requests.RequestException:
            pass
        time.sleep(0.5)
    raise RuntimeError("service never became healthy")


def start_node(script: str, **kw) -> subprocess.Popen:
    cmd = [sys.executable, str(ROOT / "scripts" / script)]
    for k, v in kw.items():
        cmd += [f"--{k.replace('_', '-')}", str(v)]
    return subprocess.Popen(cmd, cwd=ROOT, env=ENV,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)


def stop_node(p, sig=signal.SIGTERM, timeout=8):
    if p is None or p.poll() is not None:
        return
    p.send_signal(sig)
    try:
        p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait(timeout=5)


def get_diagnosis(topic, timeout=3):
    try:
        r = requests.get(f"{BASE}/api/v1/diagnosis{topic}", timeout=timeout)
        return r.json() if r.ok else None
    except requests.RequestException:
        return None


def wait_severity(topic, expected, rule=None, timeout=25, predicate=None):
    expected = (expected,) if isinstance(expected, str) else tuple(expected)
    t0, last = time.time(), None
    while time.time() - t0 < timeout:
        data = get_diagnosis(topic)
        if data:
            last = data["severity"]
            if data["severity"] in expected:
                if rule is None or any(
                        rule in m.get("blocking_rules", []) for m in data["matches"]):
                    if predicate is None or predicate(data):
                        return data
        time.sleep(0.5)
    raise AssertionError(f"{topic}: expected {expected} rule={rule}, last={last}")


def _provenance_ready(data):
    eps = data.get("publishers", []) + data.get("subscriptions", [])
    return bool(eps) and all(
        e["provenance"].get("depth") == "endpoint_announce"
        and e["provenance"].get("history") == "endpoint_announce"
        and e["qos"]["depth"] > 0 for e in eps)


def get_endpoint(topic, node_fragment):
    r = requests.get(f"{BASE}/api/v1/topology", timeout=3)
    for ep in r.json()["endpoints"]:
        if ep["topic"] == topic and node_fragment in ep["node"]:
            return ep
    return None


def wait_endpoint_state(topic, node_fragment, state, timeout=20):
    t0 = time.time()
    while time.time() - t0 < timeout:
        ep = get_endpoint(topic, node_fragment)
        if ep and ep["state"] == state:
            return ep
        time.sleep(0.3)
    raise AssertionError(f"{node_fragment} on {topic} never reached {state}")


def assert_explainable(data):
    assert data["matches"], "match list empty"
    m = data["matches"][0]
    steps = m["steps"]
    assert {s["rule_id"] for s in steps} >= {"R1", "R2", "R3", "R4"}
    for s in steps:
        assert s["rationale"] and s["decision"], "chain step incomplete"
        assert s["publisher_evidence"] and s["subscriber_evidence"], \
            "chain step missing evidence source"
    log("  match chain: " + " | ".join(
        f"{s['rule_id']}({s['attribute']})={s['decision']}" for s in steps))


def assert_provenance(data):
    for dto in data["publishers"] + data["subscriptions"]:
        prov = dto["provenance"]
        assert prov.get("reliability") == "rmw_discovery", \
            f"reliability must come from DDS discovery: {prov}"
        assert prov.get("durability") == "rmw_discovery", prov
        assert prov.get("depth") == "endpoint_announce", \
            f"depth is not on the DDS wire; must come from beacon: {prov}"
        assert prov.get("history") == "endpoint_announce", prov
    log("  provenance: reliability/durability=rmw_discovery, history/depth=endpoint_announce")


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-server", action="store_true")
    args = ap.parse_args()

    procs = []
    server = None
    if not args.no_server:
        log("starting API server (uvicorn) ...")
        server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "qos_diag.api:app",
             "--host", "127.0.0.1", "--port", PORT],
            cwd=ROOT, env=ENV, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True)
    try:
        h = wait_health()
        log(f"server healthy: mode={h['mode']} rmw={h['rmw']} grace={GRACE}s")
        assert h["mode"] == "ros2", "acceptance must run against real ROS2"

        # ---------- Phase A: reliability ----------
        log("=== Phase A: publisher BEST_EFFORT vs subscriber RELIABLE -> R1 ===")
        pubA = start_node("demo_publisher.py", name="acc_pub_A", topic=TOPIC_A,
                          runtime=120, reliability="best_effort",
                          durability="volatile", history="keep_last", depth=10)
        procs.append(pubA)
        subA = start_node("demo_subscriber.py", name="acc_sub_A_bad", topic=TOPIC_A,
                          runtime=120, reliability="reliable",
                          durability="volatile", history="keep_last", depth=10)
        procs.append(subA)
        data = wait_severity(TOPIC_A, "INCOMPATIBLE", rule="R1",
                             predicate=_provenance_ready)
        log("  INCOMPATIBLE confirmed with blocking rule R1")
        assert_explainable(data)
        assert_provenance(data)

        log("=== Phase A fix: subscriber restarted BEST_EFFORT ===")
        stop_node(subA)
        procs.remove(subA)
        subA2 = start_node("demo_subscriber.py", name="acc_sub_A_ok", topic=TOPIC_A,
                           runtime=90, reliability="best_effort",
                           durability="volatile", history="keep_last", depth=10)
        procs.append(subA2)
        data = wait_severity(TOPIC_A, ("COMPATIBLE", "RISK"))
        log(f"  recovered: severity={data['severity']}")
        assert data["severity"] == "COMPATIBLE", data["severity"]

        # ---------- Phase B: durability ----------
        log("=== Phase B: publisher VOLATILE vs subscriber TRANSIENT_LOCAL -> R2 ===")
        pubB = start_node("demo_publisher.py", name="acc_pub_B_bad", topic=TOPIC_B,
                          runtime=120, reliability="reliable",
                          durability="volatile", history="keep_last", depth=10)
        procs.append(pubB)
        subB = start_node("demo_subscriber.py", name="acc_sub_B", topic=TOPIC_B,
                          runtime=120, reliability="reliable",
                          durability="transient_local", history="keep_all", depth=10)
        procs.append(subB)
        data = wait_severity(TOPIC_B, "INCOMPATIBLE", rule="R2",
                             predicate=_provenance_ready)
        log("  INCOMPATIBLE confirmed with blocking rule R2")
        assert_explainable(data)

        log("=== Phase B fix: publisher restarted TRANSIENT_LOCAL ===")
        stop_node(pubB)
        procs.remove(pubB)
        pubB2 = start_node("demo_publisher.py", name="acc_pub_B_ok", topic=TOPIC_B,
                           runtime=90, reliability="reliable",
                           durability="transient_local", history="keep_last", depth=10)
        procs.append(pubB2)
        data = wait_severity(TOPIC_B, ("COMPATIBLE", "RISK"))
        assert data["severity"] == "RISK", data["severity"]
        assert any("R3" in m["risk_rules"] for m in data["matches"])
        log("  connects with KEEP_ALL subscriber: RISK flagged by R3 only (perf, not failure)")

        # ---------- snapshot crypto ----------
        log("=== snapshot: capture + cryptographic verification ===")
        sid = requests.post(f"{BASE}/api/v1/snapshots", timeout=5).json()["snapshot_id"]
        v = requests.get(f"{BASE}/api/v1/snapshots/{sid}/verify", timeout=5).json()
        assert v["checks"]["all_ok"], v
        log(f"  {sid}: content_sha256 + HMAC-SHA256 + rules_hash all OK")
        f = Path(ENV["QOSDIAG_DATA_DIR"]) / "snapshots" / f"{sid}.json"
        rec = json.loads(f.read_text())
        rec["body"]["note"] = "tampered"
        f.write_text(json.dumps(rec))
        v2 = requests.get(f"{BASE}/api/v1/snapshots/{sid}/verify", timeout=5).json()["checks"]
        assert not v2["content_sha256"]["ok"]
        assert not v2["hmac_sha256"]["ok"]
        assert v2["rules_hash"]["ok"]
        log("  tamper detected by both content hash and keyed HMAC; rules hash unaffected")

        # ---------- grace window ----------
        log("=== grace: SIGKILL (no DDS goodbye) -> SUSPECT then EXPIRED then ACTIVE ===")
        gtopic = f"/qos_acc/grace/run{RUN_TAG}"
        gpub = start_node("demo_publisher.py", name="acc_grace_pub", topic=gtopic,
                          runtime=120, reliability="reliable")
        procs.append(gpub)
        wait_endpoint_state(gtopic, "acc_grace_pub", "ACTIVE")
        gpub.kill(); gpub.wait()
        # DDS keeps the endpoint in the graph until the participant lease
        # (20s default) lapses after a hard kill; only then does absence begin.
        ep = wait_endpoint_state(gtopic, "acc_grace_pub", "SUSPECT",
                                 timeout=DDS_LEASE_WAIT_S)
        assert ep["absent_for_s"] < GRACE
        log(f"  DDS lease lapsed: state=SUSPECT absent_for={ep['absent_for_s']}s "
            f"(never treated as permanent failure inside the grace window)")
        # after grace -> EXPIRED (clock starts when the lease lapsed into SUSPECT)
        wait_endpoint_state(gtopic, "acc_grace_pub", "EXPIRED",
                            timeout=GRACE + 8)
        log(f"  beyond {GRACE}s grace: EXPIRED")
        procs.remove(gpub)
        gpub2 = start_node("demo_publisher.py", name="acc_grace_pub", topic=gtopic,
                           runtime=40, reliability="reliable")
        procs.append(gpub2)
        ep2 = wait_endpoint_state(gtopic, "acc_grace_pub", "ACTIVE")
        assert ep2["returns"] >= 1
        log(f"  rediscovery: ACTIVE, returns={ep2['returns']}")

        log("ALL ACCEPTANCE CHECKS PASSED")
        return 0
    finally:
        for p in procs:
            stop_node(p)
        if server is not None:
            stop_node(server)
    return 0


if __name__ == "__main__":
    sys.exit(main())
