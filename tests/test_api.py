"""End-to-end tests: HTTP API, CLI, brute-force cross-checking and proof
tampering detection. The solver is treated as a black box here; verdict
correctness uses the independent checker in sat_check.py.
"""
import http.client
import json
import os
import random
import socket
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sat_check import ValidationError, normalize_cnf, validate_response  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "build", "sat_solver")

failures = []


def check(cond, label):
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        failures.append(label)


def wait_for_port(port, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def request(port, method, path, body=None, raw=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Content-Type": "application/json"}
    payload = raw if raw is not None else json.dumps(body)
    conn.request(method, path, payload, headers)
    resp = conn.getresponse()
    data = resp.read().decode()
    conn.close()
    return resp.status, json.loads(data) if data else None


def random_formula(rng, n, m, max_len=3):
    clauses = []
    for _ in range(m):
        length = rng.randint(1, max_len)
        clause = []
        for _ in range(length):
            v = rng.randint(1, n)
            clause.append(v if rng.random() < 0.5 else -v)
        clauses.append(clause)
    return clauses


def test_http(port):
    print("[http api]")
    status, body = request(port, "GET", "/healthz")
    check(status == 200 and body["status"] == "ok", "GET /healthz")

    # (x1) and (-x1 v x2): unit propagation forces x1=true, x2=true.
    status, body = request(port, "POST", "/solve",
                           {"num_vars": 2, "clauses": [[1], [-1, 2]]})
    check(status == 200 and body["status"] == "sat", "simple sat formula")
    check(validate_response(body, 2, [[1], [-1, 2]]) == "sat",
          "independent checker accepts sat proof")

    # Empty clause -> unsat.
    status, body = request(port, "POST", "/solve", {"num_vars": 2, "clauses": [[]]})
    check(status == 200 and body["status"] == "unsat", "empty clause is unsat")
    check(validate_response(body, 2, [[]]) == "unsat", "checker accepts empty-clause proof")

    # Tautology and repeated literals normalize away -> sat.
    formula = {"num_vars": 3, "clauses": [[1, -1, 1], [2, 2], [-3, 3]]}
    status, body = request(port, "POST", "/solve", formula)
    check(status == 200 and body["status"] == "sat",
          "only tautologies/duplicates => sat (empty formula after normalization)")
    check(body["normalized_clauses"] == [[2]], "normalized clauses keeps [2]")
    check(sorted(body["tautology_clause_indices"]) == [0, 2],
          "tautology indices reported")
    check(validate_response(body, 3, formula["clauses"]) == "sat",
          "checker accepts tautology-only proof")

    # Unsat formula with branching: (a b)(-a b)(a -b)(-a -b).
    unsat = [[1, 2], [-1, 2], [1, -2], [-1, -2]]
    status, body = request(port, "POST", "/solve", {"num_vars": 2, "clauses": unsat})
    check(status == 200 and body["status"] == "unsat", "all-4 binary clauses unsat")
    check(validate_response(body, 2, unsat) == "unsat", "checker accepts unsat proof")

    # Brute reference agrees.
    status, brute = request(port, "POST", "/brute", {"num_vars": 2, "clauses": unsat})
    check(status == 200 and brute["status"] == "unsat"
          and brute["assignments_tested"] == 4, "brute reference reports unsat/4 tested")

    # /verify round-trips a valid response.
    verify_req = {"num_vars": 2, "clauses": unsat,
                  "status": body["status"], "model": body["model"],
                  "proof": body["proof"]}
    status, v = request(port, "POST", "/verify", verify_req)
    check(status == 200 and v["valid"] is True, "/verify accepts valid unsat proof")

    # Tampered proof rejected.
    tampered = json.loads(json.dumps(verify_req))
    for node in tampered["proof"]["nodes"]:
        if node["kind"] == "conflict":
            node["conflict_clause"] = 0
    status, v = request(port, "POST", "/verify", tampered)
    check(status == 200 and v["valid"] is False, "/verify rejects tampered conflict")

    # Error handling.
    status, v = request(port, "POST", "/solve", raw="{bad json")
    check(status == 400 and "error" in v, "malformed json -> 400")
    status, v = request(port, "POST", "/solve", {"num_vars": 2, "clauses": [[5]]})
    check(status == 400, "literal beyond num_vars -> 400")
    status, v = request(port, "POST", "/solve",
                        {"num_vars": 500, "clauses": []})
    check(status == 400, "num_vars over limit -> 400")
    status, v = request(port, "POST", "/nope", {})
    check(status == 404, "unknown path -> 404")
    status, _ = request(port, "GET", "/solve")
    check(status == 405, "GET on POST endpoint -> 405")

    # Node limit is reported, not silently wrong. 30 variables with no unit
    # clauses force branching; a limit of 3 cannot complete the search.
    status, body = request(port, "POST", "/solve",
                           {"num_vars": 30, "clauses": [[1, 2]], "node_limit": 3})
    check(status == 200 and body["status"] == "limit", "node limit surfaced honestly")


def test_cross_check(port):
    print("[random cross-check: dpll vs brute vs independent checker]")
    rng = random.Random(99)
    counts = {"sat": 0, "unsat": 0}
    for trial in range(250):
        n = rng.randint(1, 8)
        m = rng.randint(0, 14)
        clauses = random_formula(rng, n, m)
        _, normalized, _ = normalize_cnf(n, clauses)
        status, solved = request(port, "POST", "/solve",
                                 {"num_vars": n, "clauses": clauses})
        check_once = status == 200 and solved["status"] in ("sat", "unsat")
        if check_once:
            verdict = validate_response(solved, n, clauses)
            counts[verdict] += 1
        _, brute = request(port, "POST", "/brute", {"num_vars": n, "clauses": clauses})
        agreement = brute["status"] == solved["status"]
        if not (check_once and agreement):
            check(False, f"trial {trial} n={n} clauses={clauses}")
            return
    check(True, f"250 random formulas agree ({counts['sat']} sat, {counts['unsat']} unsat)")


def run_cli(args, stdin_text=None):
    proc = subprocess.run([BIN] + args, input=stdin_text, capture_output=True,
                          text=True, timeout=30, cwd=ROOT)
    return proc.returncode, proc.stdout, proc.stderr


def write_temp(suffix, text):
    fd, path = tempfile.mkstemp(suffix=suffix, dir=ROOT)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    return path


def test_cli():
    print("[cli]")
    dimacs = "c test\np cnf 3 3\n1 2 0\n-1 3 0\n-3 0\n"
    path = write_temp(".cnf", dimacs)
    try:
        rc, out, err = run_cli(["solve", path])
        result = json.loads(out)
        check(rc == 0 and result["status"] == "sat", "dimacs solve via file")
        check(validate_response(result, 3, [[1, 2], [-1, 3], [-3]]) == "sat",
              "cli sat result independently valid")

        result_path = write_temp(".json", out)
        rc2, out2, _ = run_cli(["verify", result_path])
        check(rc2 == 0 and json.loads(out2)["valid"] is True,
              "verify subcommand accepts own proof")
        os.unlink(result_path)

        rc, out, _ = run_cli(["solve", "-"], dimacs)
        check(rc == 0 and json.loads(out)["status"] == "sat", "dimacs solve via stdin")

        rc, out, _ = run_cli(["brute", path])
        b = json.loads(out)
        check(rc == 0 and b["status"] == "sat", "brute via file")

        # The 4-clause unsat formula needs 3 search nodes; limit 1 stops it.
        rc, out, _ = run_cli(["solve", "-", "1"],
                             "p cnf 2 4\n1 2 0\n-1 2 0\n1 -2 0\n-1 -2 0\n")
        check(rc == 3 and json.loads(out)["status"] == "limit",
              "node limit exit code 3")

        rc, _, err = run_cli(["solve", "nonexistent.cnf"])
        check(rc != 0, "missing file fails")
    finally:
        os.unlink(path)

    # Unsat pigeonhole-ish DIMACS.
    unsat_dimacs = "p cnf 2 4\n1 2 0\n-1 2 0\n1 -2 0\n-1 -2 0\n"
    rc, out, _ = run_cli(["solve", "-"], unsat_dimacs)
    result = json.loads(out)
    check(rc == 0 and result["status"] == "unsat", "unsat dimacs via stdin")
    check(validate_response(result, 2,
                            [[1, 2], [-1, 2], [1, -2], [-1, -2]]) == "unsat",
          "cli unsat proof independently valid")


def test_handcrafted_bad_proofs(port):
    print("[handcrafted invalid proofs]")
    unsat = [[1, 2], [-1, 2], [1, -2], [-1, -2]]
    _, good = request(port, "POST", "/solve", {"num_vars": 2, "clauses": unsat})

    # Invent a propagation whose reason clause is not unit.
    bad = json.loads(json.dumps(good))
    bad["proof"]["nodes"][0]["propagations"].insert(
        0, {"literal": 1, "reason_clause": 0})
    try:
        validate_response(bad, 2, unsat)
        check(False, "fake unit propagation rejected")
    except ValidationError:
        check(True, "fake unit propagation rejected")

    # SAT claim without a model.
    bad = json.loads(json.dumps(good))
    bad["status"] = "sat"
    bad["model"] = None
    try:
        validate_response(bad, 2, unsat)
        check(False, "sat claim without model rejected")
    except ValidationError:
        check(True, "sat claim without model rejected")

    # Claimed model that falsifies a clause.
    sat_formula = [[1, 2], [-1]]
    _, good_sat = request(port, "POST", "/solve",
                          {"num_vars": 2, "clauses": sat_formula})
    good_sat["model"][1]["value"] = False  # real solver forced x1=false; leave x2?
    try:
        validate_response(good_sat, 2, sat_formula)
        check(False, "model falsifying clause rejected")
    except ValidationError:
        check(True, "model falsifying clause rejected")


def main():
    if not os.path.exists(BIN):
        print(f"binary missing: {BIN}; run make first", file=sys.stderr)
        return 1
    port = 18086
    server = subprocess.Popen([BIN, "serve", str(port)],
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
    try:
        if not wait_for_port(port):
            print("server failed to start", file=sys.stderr)
            return 1
        test_http(port)
        test_cross_check(port)
        test_cli()
        test_handcrafted_bad_proofs(port)
    finally:
        server.terminate()
        server.wait(timeout=5)

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("ALL PYTHON TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
