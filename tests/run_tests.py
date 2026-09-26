#!/usr/bin/env python3
"""Automated tests for the bipartite-matching backend.

The test suite includes an INDEPENDENT naive reference (written from
scratch in Python):

  * maximum matching size by exhaustive backtracking over left vertices;
  * minimum vertex cover by enumerating vertex subsets in increasing size.

For hundreds of seeded random small graphs -- including isolated vertices
and duplicate edges -- the C++ Hopcroft-Karp/Konig output must agree with
this independent enumeration, every reported edge must touch the cover,
and the cover size must equal the matching size.

Additional sections cover: id preservation, boundary/error handling,
batch/stdin/--file modes, and the HTTP service.

Exit code is non-zero if any check fails.
"""

import http.client
import itertools
import json
import os
import random
import socket
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "build", "bipartite-matching")

passed = 0
failures = []


def check(name, condition, detail=""):
    global passed
    if condition:
        passed += 1
    else:
        failures.append((name, detail))


# ---------------------------------------------------------------------------
# Independent exhaustive reference implementations (Python, from scratch)
# ---------------------------------------------------------------------------

def py_max_matching_size(left_ids, right_ids, edges):
    adj = {u: [] for u in left_ids}
    for u, v in edges:
        adj[u].append(v)
    for u in adj:
        adj[u] = sorted(set(adj[u]))

    used_right = set()
    best = 0

    def search(position, current):
        nonlocal best
        if current > best:
            best = current
        if position == len(left_ids):
            return
        if current + (len(left_ids) - position) <= best:
            return
        u = left_ids[position]
        search(position + 1, current)  # leave u unmatched
        for v in adj[u]:
            if v not in used_right:
                used_right.add(v)
                search(position + 1, current + 1)
                used_right.remove(v)

    search(0, 0)
    return best


def py_min_vertex_cover(left_ids, right_ids, unique_edges):
    """Smallest subset (searched in increasing cardinality) covering all
    edges. Pure definition-based enumeration, no use of matching theory."""
    vertices = [("L", u) for u in left_ids] + [("R", v) for v in right_ids]
    for k in range(len(vertices) + 1):
        for combo in itertools.combinations(range(len(vertices)), k):
            chosen = {vertices[i] for i in combo}
            if all((("L", u) in chosen) or (("R", v) in chosen)
                   for u, v in unique_edges):
                return k, sorted((side, ident) for side, ident in chosen)
    raise AssertionError("unreachable")


# ---------------------------------------------------------------------------
# Process helpers
# ---------------------------------------------------------------------------

def run_cli(payload):
    proc = subprocess.run(
        [BIN], input=json.dumps(payload), capture_output=True,
        text=True, timeout=60,
    )
    try:
        response = json.loads(proc.stdout)
    except json.JSONDecodeError:
        response = None
    return proc.returncode, response, proc.stderr


def verify_response_body(name, response, left_ids, right_ids, unique_edges,
                         expect_brute=False):
    check(name + ": ok flag", response is not None and response.get("ok") is True,
          str(response)[:500])
    if not response or not response.get("ok"):
        return

    matching = [(m["left"], m["right"]) for m in response["matching"]]
    cover = [(c["side"], c["id"]) for c in response["minimum_vertex_cover"]]
    edge_set = set(unique_edges)

    # Matching pairs are genuine edges and share no endpoint.
    seen_l, seen_r, matching_ok = set(), set(), True
    for u, v in matching:
        if (u, v) not in edge_set or u in seen_l or v in seen_r:
            matching_ok = False
            break
        seen_l.add(u)
        seen_r.add(v)
    check(name + ": matching pairs valid & disjoint", matching_ok)

    # Cover touches every edge.
    cover_l = {i for s, i in cover if s == "L"}
    cover_r = {i for s, i in cover if s == "R"}
    covers_all = all((u in cover_l) or (v in cover_r)
                     for u, v in unique_edges)
    check(name + ": cover touches every edge", covers_all)

    check(name + ": cover size == matching size",
          len(cover) == len(matching) == response["matching_size"]
          == response["minimum_vertex_cover_size"])

    verification = response["verification"]
    check(name + ": verification flags",
          verification["matching_is_valid"]
          and verification["cover_touches_every_edge"]
          and verification["cover_size_equals_matching_size"]
          and verification["verified_optimal"])

    # Original vertex ids must be preserved (no internal index leakage).
    check(name + ": ids preserved",
          all(u in set(left_ids) for u, _ in matching)
          and all(v in set(right_ids) for _, v in matching)
          and all((s == "L" and i in set(left_ids))
                  or (s == "R" and i in set(right_ids))
                  for s, i in cover))

    if expect_brute:
        brute = response.get("brute_reference")
        check(name + ": brute reference present & agreeing",
              brute is not None and brute["agrees"] is True
              and brute["max_matching_size"] == len(matching)
              and brute["min_vertex_cover_size"] == len(cover),
              str(brute))


# ---------------------------------------------------------------------------
# 1. Random small graphs vs independent exhaustive enumeration
# ---------------------------------------------------------------------------

def test_random_vs_brute():
    rng = random.Random(20260925)
    cases = 0
    for iteration in range(350):
        n_left = rng.randint(0, 6)
        n_right = rng.randint(0, 6)
        # Build distinct ids explicitly; arbitrary, possibly negative.
        left_ids = []
        while len(left_ids) < n_left:
            candidate = rng.randint(-50, 200)
            if candidate not in left_ids:
                left_ids.append(candidate)
        right_ids = []
        while len(right_ids) < n_right:
            candidate = rng.randint(-50, 200)
            if candidate not in right_ids and candidate not in left_ids:
                right_ids.append(candidate)

        probability = rng.choice([0.0, 0.2, 0.35, 0.6, 0.8, 1.0])
        edges = []
        for u in left_ids:
            for v in right_ids:
                if rng.random() < probability:
                    repetition = rng.randint(1, 3)  # duplicate edges
                    edges.extend([[u, v]] * repetition)

        # Extra isolated vertices declared on purpose.
        iso_l = []
        iso_r = []
        for _ in range(rng.randint(0, 2)):
            candidate = rng.randint(1000, 2000)
            if candidate not in left_ids and candidate not in right_ids:
                iso_l.append(candidate)
        for _ in range(rng.randint(0, 2)):
            candidate = rng.randint(-2000, -1000)
            if candidate not in left_ids and candidate not in right_ids \
                    and candidate not in iso_l:
                iso_r.append(candidate)

        declared_left = left_ids + iso_l
        declared_right = right_ids + iso_r
        rng.shuffle(edges)
        payload = {
            "left": declared_left,
            "right": declared_right,
            "edges": edges,
            "brute_force": True,
        }
        code, response, stderr = run_cli(payload)
        check(f"random[{iteration}]: exit code", code == 0, stderr)

        unique_edges = sorted({(u, v) for u, v in edges})
        verify_response_body(f"random[{iteration}]", response,
                             declared_left, declared_right, unique_edges,
                             expect_brute=True)
        if not response or not response.get("ok"):
            continue

        # Isolated vertices can never belong to a minimum vertex cover;
        # exclude them from the independent enumeration to keep it fast.
        noniso_left = sorted({u for u, _ in unique_edges})
        noniso_right = sorted({v for _, v in unique_edges})
        expected_matching = py_max_matching_size(
            noniso_left, noniso_right, unique_edges)
        expected_cover_size, _ = py_min_vertex_cover(
            noniso_left, noniso_right, unique_edges)
        check(f"random[{iteration}]: matching == exhaustive {expected_matching}",
              response["matching_size"] == expected_matching,
              f"got {response['matching_size']} edges={unique_edges}")
        check(f"random[{iteration}]: cover size == exhaustive "
              f"{expected_cover_size}",
              response["minimum_vertex_cover_size"] == expected_cover_size)
        cases += 1
    check("random cases executed (>=300)", cases >= 300, str(cases))


# ---------------------------------------------------------------------------
# 2. Fixed structural examples
# ---------------------------------------------------------------------------

def test_structures():
    # Complete bipartite K_{5,5}: optimum 5.
    edges = [[u, v] for u in range(5) for v in range(100, 105)]
    code, response, err = run_cli({
        "left": list(range(5)), "right": list(range(100, 105)),
        "edges": edges, "brute_force": True})
    check("K5,5 exit", code == 0, err)
    verify_response_body("K5,5", response, list(range(5)),
                         list(range(100, 105)),
                         sorted({(u, v) for u, v in edges}), True)
    check("K5,5 optimum 5", response and response["matching_size"] == 5)

    # Star: one left connected to 5 rights -> optimum 1, cover = center.
    code, response, err = run_cli({
        "left": [0], "right": [1, 2, 3, 4, 5],
        "edges": [[0, v] for v in range(1, 6)], "brute_force": True})
    check("star optimum 1", response and response["matching_size"] == 1)
    check("star cover is the center",
          response and
          [(c["side"], c["id"]) for c in response["minimum_vertex_cover"]]
          == [("L", 0)], str(response))

    # Even cycle C8 (4 left, 4 right, 8 edges): optimum 4.
    cyc_edges = [(0, 0), (0, 1), (1, 1), (1, 2), (2, 2), (2, 3), (3, 3),
                 (3, 0)]
    code, response, err = run_cli({
        "left": [10, 11, 12, 13], "right": [20, 21, 22, 23],
        "edges": [[10 + u, 20 + v] for u, v in cyc_edges]})
    check("C8 optimum 4", response and response["matching_size"] == 4, err)
    check("C8 cover size 4",
          response and response["minimum_vertex_cover_size"] == 4)

    # No declared sides: edges alone define the bipartition.
    code, response, err = run_cli({"edges": [[5, 6], [5, 7], [8, 7]]})
    check("implicit sides exit", code == 0, err)
    verify_response_body("implicit sides", response, [5, 8], [6, 7],
                         [(5, 6), (5, 7), (8, 7)])

    # Completely empty request.
    code, response, err = run_cli({})
    check("empty request exit", code == 0, err)
    check("empty request zero optimum",
          response and response["matching_size"] == 0
          and response["minimum_vertex_cover_size"] == 0
          and response["matching"] == []
          and response["minimum_vertex_cover"] == [])

    # Only isolated vertices.
    code, response, err = run_cli({"left": [1], "right": [2], "edges": []})
    check("isolated-only exit", code == 0, err)
    check("isolated-only counts",
          response and response["graph"]["left_count"] == 1
          and response["graph"]["right_count"] == 1
          and response["graph"]["edge_count"] == 0
          and response["matching_size"] == 0
          and response["minimum_vertex_cover"] == []
          and response["verification"]["verified_optimal"] is True)


def test_duplicates_and_warnings():
    payload = {
        "left": [1, 2], "right": [3, 4],
        "edges": [[1, 3], [1, 3], [1, 3], [2, 3], [2, 4], [2, 4]],
        "brute_force": True,
    }
    code, response, err = run_cli(payload)
    check("duplicate edges exit", code == 0, err)
    verify_response_body("duplicate edges", response, [1, 2], [3, 4],
                         [(1, 3), (2, 3), (2, 4)], True)
    check("duplicate edges edge_count deduped",
          response and response["graph"]["edge_count"] == 3)
    check("duplicate edges warning",
          response and any("duplicate edges" in w for w in response["warnings"]))

    payload_dup_ids = {"left": [1, 1, 2], "right": [3], "edges": [[1, 3]]}
    code, response, err = run_cli(payload_dup_ids)
    check("duplicate declared ids exit", code == 0, err)
    check("duplicate declared ids warning",
          response and any("deduplicated" in w for w in response["warnings"]))


# ---------------------------------------------------------------------------
# 3. Error handling and limits
# ---------------------------------------------------------------------------

def test_errors():
    # Malformed JSON via direct stdin.
    proc = subprocess.run([BIN], input="{not json", capture_output=True,
                          text=True)
    response = json.loads(proc.stdout)
    check("invalid JSON rejected", proc.returncode != 0
          and response["ok"] is False
          and response["error"]["code"] == "INVALID_JSON")

    code, response, _ = run_cli({"left": [1], "right": [2],
                                 "edges": [{"left": 1, "right": 9}]})
    check("undeclared right endpoint rejected",
          response["ok"] is False
          and response["error"]["code"] == "INVALID_REQUEST")

    code, response, _ = run_cli({"left": [1, 2], "right": [2, 3],
                                 "edges": [[1, 2]]})
    check("overlapping side ids rejected",
          response["ok"] is False
          and response["error"]["code"] == "INVALID_REQUEST")

    code, response, _ = run_cli({"edges": [{"left": "a", "right": 2}]})
    check("string id rejected", response["ok"] is False)

    code, response, _ = run_cli({"edges": [{"left": 1.5, "right": 2}]})
    check("fractional id rejected", response["ok"] is False)

    code, response, _ = run_cli({"edges": "nope"})
    check("non-array edges rejected", response["ok"] is False)

    code, response, _ = run_cli({"left": [1], "right": [2],
                                 "edges": [[1, 2]], "brute_force": "yes"})
    check("non-boolean brute_force rejected", response["ok"] is False)

    # 2^53 is allowed, 2^53 + 1 is rejected (safe-integer boundary).
    code, response, _ = run_cli({"edges": [[9007199254740992, 1]]})
    check("id == 2^53 accepted", response["ok"] is True, str(response))
    code, response, _ = run_cli({"edges": [[9007199254740993, 1]]})
    check("id == 2^53+1 rejected", response["ok"] is False)

    # brute_force beyond the naive limit: core result still served, but
    # the brute switch produces a structured error.
    many = [[u, v] for u in range(11) for v in range(11)]
    code, response, _ = run_cli({"edges": many, "brute_force": True})
    check("brute force over limit rejected",
          response["ok"] is False
          and response["error"]["code"] == "BRUTE_FORCE_TOO_LARGE")

    # Too many edges.
    big_edges = [[0, i] for i in range(200001)]
    code, response, _ = run_cli({"left": [0], "right": list(range(200001)),
                                 "edges": big_edges})
    check("edge limit enforced",
          response["ok"] is False
          and response["error"]["code"] == "LIMIT_EXCEEDED",
          str(response)[:200])

    # Too many vertices on one side.
    code, response, _ = run_cli({"left": list(range(10001)),
                                 "right": [-1], "edges": []})
    check("vertex limit enforced",
          response["ok"] is False
          and response["error"]["code"] == "LIMIT_EXCEEDED")


# ---------------------------------------------------------------------------
# 4. Batch and file modes
# ---------------------------------------------------------------------------

def test_batch_and_files():
    batch = [
        {"left": [0, 1], "right": [2, 3],
         "edges": [[0, 2], [1, 3]], "brute_force": True},
        {"left": [9], "right": [8], "edges": []},
    ]
    proc = subprocess.run([BIN], input=json.dumps(batch),
                          capture_output=True, text=True)
    response = json.loads(proc.stdout)
    check("batch returns array", isinstance(response, list) and len(response) == 2)
    check("batch both ok",
          all(item["ok"] for item in response))
    check("batch values",
          response[0]["matching_size"] == 2
          and response[1]["matching_size"] == 0)
    check("batch exit code 0", proc.returncode == 0)

    batch_bad = [{"left": [0], "right": [1], "edges": [[0, 1]]},
                 {"left": [0], "right": [0], "edges": []}]
    proc = subprocess.run([BIN], input=json.dumps(batch_bad),
                          capture_output=True, text=True)
    check("batch with one invalid exits non-zero", proc.returncode != 0)
    response = json.loads(proc.stdout)
    check("batch partial failure preserved",
          response[0]["ok"] is True and response[1]["ok"] is False)

    path = os.path.join(ROOT, "examples", "request_basic.json")
    proc = subprocess.run([BIN, "--file", path], capture_output=True, text=True)
    check("--file exit 0", proc.returncode == 0, proc.stderr)
    line = json.loads(proc.stdout.strip())
    check("--file envelope", line["file"].endswith("request_basic.json")
          and line["response"]["ok"] is True
          and line["response"]["brute_reference"]["agrees"] is True)


# ---------------------------------------------------------------------------
# 5. Larger instance smoke test for the size bound
# ---------------------------------------------------------------------------

def test_larger_instance():
    rng = random.Random(42)
    n_left = n_right = 2000
    edges = []
    for u in range(n_left):
        for v in range(n_right):
            if rng.random() < 0.0002:  # ~800 edges, sparse
                edges.append([u, 10000 + v])
    started = time.time()
    code, response, err = run_cli({"edges": edges})
    elapsed = time.time() - started
    check("large graph exit", code == 0, err)
    check("large graph verified",
          response and response["verification"]["verified_optimal"] is True)
    check("large graph ids preserved (>= 10000 offset)",
          response and all(p["right"] >= 10000 for p in response["matching"]))
    print(f"\n  info: graph 2000x2000 with {len(edges)} edges solved in "
          f"{elapsed:.3f}s, matching={response and response['matching_size']}, "
          f"solver_us={response and response['elapsed_us']}")


# ---------------------------------------------------------------------------
# 6. HTTP service
# ---------------------------------------------------------------------------

def wait_for_port(port, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def http_request(port, method, path, body=None, raw_body=None,
                 content_type="application/json"):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    payload = raw_body if raw_body is not None else (
        json.dumps(body) if body is not None else None)
    headers = {"Content-Type": content_type} if payload is not None else {}
    connection.request(method, path, body=payload, headers=headers)
    response = connection.getresponse()
    data = response.read().decode("utf-8")
    connection.close()
    return response.status, json.loads(data) if data else None


def test_http():
    port = 18000 + (os.getpid() % 20000)
    proc = subprocess.Popen(
        [BIN, "serve", "--host", "127.0.0.1", "--port", str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        started = wait_for_port(port)
        check("server starts", started)
        if not started:
            return

        status, body = http_request(port, "GET", "/health")
        check("GET /health 200", status == 200 and body["status"] == "ok")

        payload = {"left": [1, 2, 3], "right": [4, 5],
                   "edges": [[1, 4], [1, 5], [2, 4], [3, 5], [3, 5]],
                   "brute_force": True}
        status, body = http_request(port, "POST", "/solve", payload)
        check("POST /solve 200", status == 200 and body["ok"] is True)
        check("POST /solve optimum 2", body["matching_size"] == 2)
        check("POST /solve certificate",
              body["verification"]["verified_optimal"] is True
              and body["brute_reference"]["agrees"] is True
              and any("duplicate edges" in w for w in body["warnings"]))

        status, body = http_request(port, "GET", "/solve")
        check("GET /solve -> 405", status == 405
              and body["error"]["code"] == "METHOD_NOT_ALLOWED")

        status, body = http_request(port, "POST", "/nope")
        check("unknown path -> 404", status == 404
              and body["error"]["code"] == "NOT_FOUND")

        status, body = http_request(port, "POST", "/solve",
                                    raw_body="{broken",
                                    content_type="application/json")
        check("invalid JSON over HTTP -> 400", status == 400
              and body["error"]["code"] == "INVALID_JSON")

        status, body = http_request(port, "POST", "/solve",
                                    body={"left": [1], "right": [1],
                                          "edges": []})
        check("invalid request over HTTP -> 400", status == 400
              and body["error"]["code"] == "INVALID_REQUEST")

        status, body = http_request(port, "POST", "/solve",
                                    body={"left": list(range(10001)),
                                          "right": [-1], "edges": []})
        check("limit exceeded over HTTP -> 413", status == 413
              and body["error"]["code"] == "LIMIT_EXCEEDED")

        status, body = http_request(
            port, "POST", "/solve",
            raw_body=json.dumps({"left": [0], "right": [1], "edges": []}),
            content_type="application/json")
        check("valid minimal POST", status == 200 and body["ok"] is True)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main():
    if not os.path.exists(BIN):
        print(f"binary not found at {BIN}; run `make` first", file=sys.stderr)
        return 2

    sections = [
        ("random graphs vs exhaustive enumeration", test_random_vs_brute),
        ("fixed structural examples", test_structures),
        ("duplicate edges and warnings", test_duplicates_and_warnings),
        ("error handling and limits", test_errors),
        ("batch / file modes", test_batch_and_files),
        ("larger instance smoke test", test_larger_instance),
        ("HTTP service", test_http),
    ]
    for title, fn in sections:
        print(f"[run] {title}")
        before = passed
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - record as failure
            failures.append((title, f"unexpected exception: {exc!r}"))
        print(f"       +{passed - before} checks passed")

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s) failed, {passed} passed")
        for name, detail in failures:
            print(f"  - {name}: {detail[:400]}")
        return 1
    print(f"ALL TESTS PASSED: {passed} checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
