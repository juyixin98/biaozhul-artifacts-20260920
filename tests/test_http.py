#!/usr/bin/env python3
"""End-to-end HTTP acceptance tests for pcr-server (stdlib only).

Starts the server itself on an ephemeral port, or uses $PCR_BASE_URL.
Exercises: health, known-transform accuracy over HTTP, outlier rejection,
collinear and no-overlap fixtures, non-finite filtering, 5000-point cap,
and real SHA-256 request/response integrity checks.
"""
import hashlib
import http.client
import json
import math
import os
import random
import signal
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVER = os.path.join(ROOT, "build", "pcr-server")

PASS, FAIL = 0, 0


def check(cond, label, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}  {detail}")


def rot_axis(axis, angle):
    x, y, z = axis
    n = math.sqrt(x * x + y * y + z * z)
    x, y, z = x / n, y / n, z / n
    c, s, C = math.cos(angle), math.sin(angle), 1 - math.cos(angle)
    return [
        [c + x * x * C, x * y * C - z * s, x * z * C + y * s],
        [y * x * C + z * s, c + y * y * C, y * z * C - x * s],
        [z * x * C - y * s, z * y * C + x * s, c + z * z * C],
    ]


def matvec(M, v):
    return [sum(M[i][j] * v[j] for j in range(3)) for i in range(3)]


def cube(n, scale=2.0, seed=1):
    rng = random.Random(seed)
    return [[rng.uniform(-scale / 2, scale / 2) for _ in range(3)] for _ in range(n)]


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Client:
    def __init__(self, base):
        self.base = base
        host, _, port = base.partition(":")
        self.host, self.port = host, int(port)

    def request(self, method, path, body=None, headers=None, raw=None):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=30)
        data = raw if raw is not None else (json.dumps(body) if body is not None else None)
        conn.request(method, path, body=data, headers=headers or {})
        resp = conn.getresponse()
        payload = resp.read().decode()
        hdr = resp.getheader("X-Content-SHA256")
        conn.close()
        return resp.status, payload, hdr

    def post(self, path, body, headers=None):
        return self.request("POST", path, body=body, headers=headers)


def wait_ready(base, proc, timeout=10):
    host, _, port = base.partition(":")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("server exited early")
        try:
            with socket.create_connection((host, int(port)), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("server did not become ready")


def main():
    base = os.environ.get("PCR_BASE_URL")
    proc = None
    if not base:
        if not os.path.exists(SERVER):
            print(f"server binary missing: {SERVER}; build first", file=sys.stderr)
            return 2
        port = free_port()
        base = f"127.0.0.1:{port}"
        proc = subprocess.Popen([SERVER, "--host", "127.0.0.1", "--port", str(port), "--threads", "2"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            wait_ready(base, proc)
            run(base)
        finally:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=5)
    else:
        base = base.replace("http://", "")
        run(base)

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


def run(base):
    c = Client(base)

    print("[health]")
    st, body, _ = c.request("GET", "/healthz")
    j = json.loads(body)
    check(st == 200 and j["status"] == "ok", "healthz 200 ok", body)

    print("[crypto: sha256 integrity]")
    msg = b'{"hello":"integrity"}'
    st, body, _ = c.request("POST", "/v1/verify-hash", raw=msg.decode())
    j = json.loads(body)
    check(j["sha256"] == hashlib.sha256(msg).hexdigest(),
          "server SHA-256 matches client", j.get("sha256", ""))

    print("[known transform over HTTP]")
    rng = random.Random(20260923)
    n = 400
    tgt = cube(n, 2.0, seed=77)
    R = rot_axis((0.3, 0.7, 0.2), 0.35)
    t = [0.15, -0.10, 0.08]
    src = [[a + b for a, b in zip(matvec(R, p), t)] for p in tgt]
    req = {"source": src, "target": tgt, "max_iterations": 100,
           "max_correspondence_distance": 0.5}
    raw = json.dumps(req)
    st, body, rhash = c.request(
        "POST", "/v1/register", raw=raw,
        headers={"Content-Type": "application/json",
                 "X-Content-SHA256": hashlib.sha256(raw.encode()).hexdigest()})
    j = json.loads(body)
    check(st == 200, "register 200", body[:200])
    check(rhash == hashlib.sha256(body.encode()).hexdigest(),
          "response X-Content-SHA256 matches body")
    check(j["converged"] and j["high_confidence"], "converged + high_confidence",
          j.get("convergence_reason", ""))
    check(j["residual_rmse"] < 1e-7, f"rmse < 1e-7 (got {j['residual_rmse']:.2e})")
    check(j["inlier_ratio"] > 0.95, f"inlier_ratio > 0.95 (got {j['inlier_ratio']:.3f})")
    check(abs(j["rotation_det"] - 1.0) < 1e-9, "det(R)=1")
    check(j["orthogonality_error"] < 1e-9, "R orthogonal")
    Rt = [list(row) for row in zip(*R)]
    errR = math.sqrt(sum((j["rotation"][i][k] - Rt[i][k]) ** 2
                         for i in range(3) for k in range(3)))
    errt = math.sqrt(sum((j["translation"][i] - (-sum(R[k][i] * t[k] for k in range(3)))) ** 2
                         for i in range(3)))
    # Reproducible error bound on this deterministic fixture.
    check(errR < 1e-6, f"rotation Frobenius error < 1e-6 (got {errR:.2e})")
    check(errt < 1e-6, f"translation error < 1e-6 (got {errt:.2e})")
    check(j["points"]["source_dropped_nonfinite"] == 0, "no finite points dropped")

    print("[outliers + non-finite over HTTP]")
    src_o = []
    rng2 = random.Random(5)
    for i, p in enumerate(src):
        if i % 4 == 0:
            src_o.append([rng2.uniform(-50, 50) for _ in range(3)])
        else:
            src_o.append(p)
    # Two non-finite rows using the accepted NaN/Infinity JSON extension.
    raw = json.dumps({"source": src_o + [["NaN", 1, 2]], "target": tgt,
                      "max_iterations": 100, "max_correspondence_distance": 0.3})
    raw = raw.replace('"NaN"', "NaN")  # emit bare NaN token
    st, body, _ = c.request("POST", "/v1/register", raw=raw,
                            headers={"Content-Type": "application/json"})
    j = json.loads(body)
    check(st == 200 and j["converged"], "converges despite 25% outliers", body[:200])
    check(j["points"]["source_finite"] == len(src_o), "NaN row dropped (finite count)")
    check(j["points"]["source_dropped_nonfinite"] == 1, "reports 1 non-finite dropped")
    check(j["residual_rmse"] < 1e-5, f"rmse small (got {j['residual_rmse']:.2e})")
    check(0.7 <= j["inlier_ratio"] <= 0.8,
          f"inlier ratio ~75% (got {j['inlier_ratio']:.3f})")

    print("[collinear fixture must not be overconfident]")
    line = [[0.01 * i, 0.0, 0.0] for i in range(100)]
    Rl = rot_axis((1, 0, 0), 0.6)
    line_src = [[a + b for a, b in zip(matvec(Rl, p), [0.05, 0, 0])] for p in line]
    st, body, _ = c.post("/v1/register",
                         {"source": line_src, "target": line, "max_iterations": 100})
    j = json.loads(body)
    check(j["convergence_reason"] == "degenerate_geometry",
          f"reason=degenerate_geometry (got {j['convergence_reason']})")
    check(j["degenerate"] and not j["high_confidence"], "degenerate, not high_confidence")
    check(abs(j["rotation_det"] - 1.0) < 1e-9, "still returns proper rotation")

    print("[no-overlap fixture]")
    far = [[v + 100 for v in p] for p in tgt[:200]]
    st, body, _ = c.post("/v1/register",
                         {"source": far, "target": tgt[:200],
                          "max_iterations": 30, "max_correspondence_distance": 0.5})
    j = json.loads(body)
    check(j["convergence_reason"] == "no_overlap",
          f"reason=no_overlap (got {j['convergence_reason']})")
    check(not j["high_confidence"] and j["correspondences"] == 0,
          "zero correspondences, no false confidence")

    print("[tampered content hash rejected]")
    st, _, _ = c.request("POST", "/v1/register", raw=raw,
                         headers={"Content-Type": "application/json",
                                  "X-Content-SHA256": "0" * 64})
    check(st == 422, f"tampered hash -> 422 (got {st})")

    print("[input validation]")
    st, _, _ = c.post("/v1/register", {"source": [[1, 2, 3]] * 5001, "target": tgt})
    check(st == 400, f">5000 points -> 400 (got {st})")
    # Exactly the cap must be accepted (use mutually overlapping tiny clouds).
    cap = [[((i * 7) % 17) * 0.01, ((i * 13) % 19) * 0.01, ((i * 5) % 23) * 0.01]
           for i in range(5000)]
    st, body, _ = c.post("/v1/register", {"source": cap, "target": cap, "max_iterations": 5})
    j = json.loads(body)
    check(st == 200 and j["points"]["source_finite"] == 5000,
          f"exactly 5000 points accepted (got {st})")
    st, _, _ = c.post("/v1/register", {"source": [[1, 2]], "target": tgt})
    check(st == 400, f"2D point -> 400 (got {st})")
    st, _, _ = c.request("POST", "/v1/register", raw="{not json",
                         headers={"Content-Type": "application/json"})
    check(st == 400, f"malformed JSON -> 400 (got {st})")
    st, _, _ = c.request("GET", "/v1/register")
    check(st == 405, f"GET register -> 405 (got {st})")
    st, _, _ = c.request("GET", "/does-not-exist")
    check(st == 404, f"unknown path -> 404 (got {st})")

    print("[payload limits]")
    chunk = b"[1,2,3],"
    big = b'{"source":[' + chunk * 400000 + b'[1,2,3]],"target":[[0,0,0]]}'
    st, _, _ = c.request("POST", "/v1/register", raw=big.decode(),
                         headers={"Content-Type": "application/json"})
    check(st == 413, f"oversized body -> 413, no RST (got {st})")

    print("[determinism: identical request twice]")
    r1 = c.post("/v1/register",
                {"source": src, "target": tgt, "max_iterations": 50,
                 "max_correspondence_distance": 0.5})[1]
    r2 = c.post("/v1/register",
                {"source": src, "target": tgt, "max_iterations": 50,
                 "max_correspondence_distance": 0.5})[1]
    check(r1 == r2, "byte-identical responses for identical inputs")


if __name__ == "__main__":
    sys.exit(main())
