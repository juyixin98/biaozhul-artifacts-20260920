#!/usr/bin/env python3
"""End-to-end acceptance driver for the resumable DAG executor.

Runs the black-box scenarios against a live server over HTTP and asserts
results. Server restart is driven by this script itself: it sends SIGTERM
to a pid file it is told about, waits, and checks state after relaunch
(which the caller performs). For the no-restart half, use --phase pre.

Usage:
  accept_e2e.py pre  <base-url> <workdir>
  accept_e2e.py post <base-url> <workdir>
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error

BASE = sys.argv[2].rstrip("/")
WORK = sys.argv[3]
PHASE = sys.argv[1]

PASS = 0
FAIL = 0


def ok(msg):
    global PASS
    PASS += 1
    print(f"PASS: {msg}")


def bad(msg):
    global FAIL
    FAIL += 1
    print(f"FAIL: {msg}")


def check(cond, msg):
    ok(msg) if cond else bad(msg)


def req(method, path, body=None, timeout=10):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"raw": raw.decode()}


def wait_dag(did, timeout=10):
    _, st = req("GET", f"/dags/{did}/wait?timeout={timeout}s")
    return st


def node(st, nid, field):
    return st["nodes"][nid].get(field, "")


def example(name):
    path = os.path.join(os.path.dirname(__file__), name)
    with open(path) as f:
        return json.load(f)


# ------------------------------------------------------------ pre-restart

if PHASE == "pre":
    print("== Scenario A: diamond A->{B,C}->E, B fails permanently ==")
    code, st = req("POST", "/dags", example("diamond_fail.json"))
    check(code == 201, f"submit accepted ({code})")
    aid = st["id"]
    st = wait_dag(aid)
    with open(os.path.join(WORK, "a_id.txt"), "w") as f:
        f.write(aid)

    check(st["status"] == "failed", f"dag status=failed (got {st['status']})")
    check(node(st, "A", "status") == "success", "A success")
    check(node(st, "A", "total_runs") == 1, "A ran exactly once")
    check(node(st, "C", "status") == "success", "C succeeds on independent branch")
    check(node(st, "C", "result") == 7, "C reused A's cached result (7)")
    check(node(st, "B", "status") == "failed", "B failed")
    check(node(st, "B", "total_runs") == 2, "B retried exactly twice (finite retry)")
    check(node(st, "E", "status") == "blocked", "E blocked, never ran")
    check(node(st, "E", "total_runs") == 0, "E total_runs=0")
    check(st.get("fail_node") == "B", "fail_node recorded as B")

    print("\n== Scenario B: cancel must not launch downstream ==")
    code, st = req("POST", "/dags", example("cancel_demo.json"))
    bid = st["id"]
    with open(os.path.join(WORK, "b_id.txt"), "w") as f:
        f.write(bid)
    time.sleep(0.4)
    code, _ = req("POST", f"/dags/{bid}/cancel")
    check(code == 202, f"cancel accepted ({code})")
    st = wait_dag(bid)
    check(st["status"] == "cancelled", "dag cancelled")
    check(node(st, "A", "total_runs") == 1, "running A interrupted after 1 run")
    check(node(st, "A", "status") == "cancelled", "A marked cancelled")
    check(node(st, "B", "total_runs") == 0, "B never launched after cancel")
    check(node(st, "C", "total_runs") == 0, "C never launched after cancel")
    check(node(st, "B", "status") == "cancelled", "B marked cancelled (not just pending)")

    print("\n== Scenario C: validation ==")
    code, st = req("POST", "/dags", {"nodes": [
        {"id": "a", "task": "noop", "deps": ["b"]},
        {"id": "b", "task": "noop", "deps": ["a"]}]})
    check(code == 400 and "cycle" in st["error"], f"cycle rejected 400 ({code})")

    code, st = req("POST", "/dags", {"nodes": [
        {"id": "a", "task": "rm -rf /", "deps": ["ghost"]}]})
    check(code == 400, f"bad spec rejected 400 ({code})")
    errs = " ".join(st.get("details", []))
    check("whitelist" in errs, "non-whitelist task reported")
    check("missing node" in errs, "missing dependency reported")

    code, _ = req("GET", "/dags/doesnotexist")
    check(code == 404, f"unknown dag -> 404 ({code})")

    print("\n== Scenario D: finite retries + backoff, result cache ==")
    t0 = time.time()
    code, st = req("POST", "/dags", example("diamond_flaky.json"))
    fid = st["id"]
    st = wait_dag(fid, 15)
    elapsed = time.time() - t0
    check(st["status"] == "succeeded", f"flaky diamond eventually succeeded ({st['status']})")
    check(node(st, "B", "total_runs") == 3, f"B used all 3 attempts (got {node(st, 'B', 'total_runs')})")
    check(node(st, "B", "result") == 32, "B result 32")
    check(node(st, "E", "result") == 42, "E = B(32)+C(10) via cached upstream results")
    check(0.5 < elapsed < 3, f"backoff observed (~0.6s), elapsed={elapsed:.2f}s")

# ------------------------------------------------------------ post-restart

if PHASE == "post":
    with open(os.path.join(WORK, "a_id.txt")) as f:
        aid = f.read().strip()
    with open(os.path.join(WORK, "b_id.txt")) as f:
        bid = f.read().strip()

    print("== After restart: scenario A state preserved, no re-execution ==")
    # Give the new process a moment in which a buggy implementation might
    # have re-launched something.
    time.sleep(1.0)
    _, st = req("GET", f"/dags/{aid}")
    check(st["status"] == "failed", f"dag still failed (got {st['status']})")
    check(node(st, "A", "total_runs") == 1 and node(st, "A", "status") == "success",
          "A not re-executed after restart (total_runs=1, success)")
    check(node(st, "C", "total_runs") == 1 and node(st, "C", "status") == "success",
          "C not re-executed after restart (total_runs=1, success)")
    check(node(st, "B", "total_runs") == 2 and node(st, "B", "status") == "failed",
          "B still failed with exactly 2 lifetime runs")
    check(node(st, "E", "total_runs") == 0 and node(st, "E", "status") == "blocked",
          "E still blocked, never executed")
    check(node(st, "A", "result") == 7, "A cached result 7 survived restart")
    check(node(st, "C", "result") == 7, "C cached result 7 survived restart")

    print("\n== After restart: cancelled DAG stays cancelled ==")
    _, st = req("GET", f"/dags/{bid}")
    check(st["status"] == "cancelled", f"cancelled dag not resurrected (got {st['status']})")
    check(node(st, "B", "total_runs") == 0, "B still never ran")
    check(node(st, "C", "total_runs") == 0, "C still never ran")

    print("\n== After restart: retry the failed diamond, successes kept ==")
    # B is a hard "fail" task, so change strategy: retry then it still fails;
    # instead verify retry keeps A/C cached. Then test flaky recovery DAG.
    code, _ = req("POST", f"/dags/{aid}/retry")
    check(code == 202, f"retry of failed dag accepted ({code})")
    st = wait_dag(aid, 5)
    check(node(st, "A", "total_runs") == 1, "A still not re-run even after retry")
    check(node(st, "C", "total_runs") == 1, "C still not re-run even after retry")
    check(node(st, "B", "total_runs") == 4, "B got 2 more runs in new activation (lifetime 4)")

    print("\n== Post-restart fresh flaky DAG proves scheduling still works ==")
    code, st = req("POST", "/dags", {
        "max_attempts": 2,
        "nodes": [
            {"id": "A", "task": "flaky", "params": {"fail_times": 1, "succeed_with": 3}},
            {"id": "B", "task": "add", "deps": ["A"]},
        ]})
    st = wait_dag(st["id"], 10)
    check(st["status"] == "succeeded", f"new dag succeeds after restart ({st['status']})")
    check(node(st, "B", "result") == 3, "upstream result cached after restart too")

print(f"\nSUMMARY: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
