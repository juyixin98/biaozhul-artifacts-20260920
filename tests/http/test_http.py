#!/usr/bin/env python3
"""End-to-end HTTP tests for the TF coordinate-tree server.

Starts the real compiled binary on a random port, performs real HTTP and
real HMAC-SHA256 request signing, and checks results against hand-derived
analytical values. Stdlib only.
"""
import hashlib
import hmac
import json
import math
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.parse

BINARY = os.environ.get("TF_SERVER")
if not BINARY:
    print("TF_SERVER env var must point to the compiled server")
    sys.exit(2)

HOST = "127.0.0.1"
KEY = "test-secret-key-2026"
TOL_SEC = "60"

FAILURES = []
SUCCESSES = []


def check(name, cond, detail=""):
    if cond:
        SUCCESSES.append(name)
        print(f"[ok]   {name}")
    else:
        FAILURES.append((name, detail))
        print(f"[FAIL] {name}  {detail}")


def free_port():
    s = socket.socket()
    s.bind((HOST, 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_ready(port, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((HOST, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.05)
    return False


class Client:
    def __init__(self, port, key=KEY):
        self.port = port
        self.key = key

    def _request(self, method, path, body_bytes, headers, want_raw=False):
        if body_bytes is None:
            body_bytes = b""
        with socket.create_connection((HOST, self.port), timeout=5) as s:
            req = (
                f"{method} {path} HTTP/1.1\r\n"
                f"Host: {HOST}:{self.port}\r\n"
                f"Content-Length: {len(body_bytes)}\r\n"
                f"Connection: close\r\n"
            )
            for k, v in headers.items():
                req += f"{k}: {v}\r\n"
            req += "\r\n"
            s.sendall(req.encode() + body_bytes)
            chunks = []
            while True:
                d = s.recv(65536)
                if not d:
                    break
                chunks.append(d)
        raw = b"".join(chunks)
        head, _, payload = raw.partition(b"\r\n\r\n")
        status_line = head.split(b"\r\n", 1)[0].decode()
        code = int(status_line.split()[1])
        try:
            parsed = json.loads(payload.decode())
        except Exception:
            parsed = None
        return code, (raw if want_raw else parsed)

    def signed(self, method, path, obj=None, extra_ts=None, bad_sig=False):
        body = b"" if obj is None else json.dumps(obj).encode()
        ts = extra_ts or str(int(time.time()))
        mac = hmac.new(
            self.key.encode(),
            f"{method}\n{path}\n{ts}\n".encode() + body,
            hashlib.sha256,
        )
        sig = "0" * 64 if bad_sig else mac.hexdigest()
        headers = {
            "Content-Type": "application/json",
            "X-TF-Timestamp": ts,
            "X-TF-Signature": sig,
        }
        return self._request(method, path, body, headers)

    def unsigned(self, method, path, obj=None):
        body = b"" if obj is None else json.dumps(obj).encode()
        return self._request(
            method, path, body, {"Content-Type": "application/json"}
        )


def q_axis_angle(axis, angle):
    norm = math.sqrt(sum(c * c for c in axis))
    ax, ay, az = (c / norm for c in axis)
    h = angle / 2.0
    s = math.sin(h)
    return {"w": math.cos(h), "x": ax * s, "y": ay * s, "z": az * s}


def approx(a, b, tol=1e-7):
    return abs(a - b) <= tol


def main():
    port = free_port()
    proc = subprocess.Popen(
        [BINARY, "--port", str(port), "--hmac-key", KEY,
         "--timestamp-tolerance", TOL_SEC],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        if not wait_ready(port):
            print("server failed to start")
            print(proc.stderr.read().decode())
            return 2
        c = Client(port)

        # ---- health (no auth) ----
        code, body = c.unsigned("GET", "/healthz")
        check("healthz 200 unauthenticated",
              code == 200 and body and body.get("status") == "ok",
              f"{code} {body}")

        # ---- auth enforcement ----
        code, body = c.unsigned("POST", "/v1/static", {"parent": "a", "child": "b"})
        check("unsigned mutation rejected 401", code == 401, str(body))

        ts = str(int(time.time()))
        code, body = c.signed("POST", "/v1/static",
                              {"parent": "a", "child": "b"}, bad_sig=True)
        check("bad signature rejected 401", code == 401, str(body))

        old = str(int(time.time()) - int(TOL_SEC) * 2)
        code, body = c.signed("POST", "/v1/static",
                              {"parent": "a", "child": "b"}, extra_ts=old)
        check("stale timestamp rejected 401", code == 401, str(body))

        # ---- static chain + analytic inverse (over HTTP) ----
        static = {
            "parent": "world", "child": "a",
            "transform": {
                "translation": [1, 0, 0],
                "rotation": q_axis_angle((0, 0, 1), math.pi / 2),
            },
        }
        code, body = c.signed("POST", "/v1/static", static)
        check("add static world->a", code == 200, str(body))

        code, body = c.signed("POST", "/v1/static", {
            "parent": "a", "child": "b",
            "transform": {
                "translation": [0, 2, 0],
                "rotation": q_axis_angle((1, 0, 0), math.pi / 2),
            },
        })
        check("add static a->b", code == 200, str(body))

        q = urllib.parse.urlencode({"from": "world", "to": "b", "time_us": 0})
        code, body = c.signed("GET", f"/v1/query?{q}")
        t = body["transform"]["translation"]
        m = body["transform"]["matrix"]
        expect_R = [[0, -1, 0], [0, 0, -1], [1, 0, 0]]
        matrix_ok = all(
            approx(m[i][j], expect_R[i][j], 1e-7)
            for i in range(3) for j in range(3)
        )
        check("HTTP static chain analytic t=(1,2,0)",
              code == 200 and all(approx(t[i], v, 1e-7)
                                  for i, v in enumerate((1, 2, 0))) and matrix_ok,
              f"t={t}")

        # analytic inverse: R^T*(1,2,0)=(0,-1,-2), negated -> (0,1,2)
        q = urllib.parse.urlencode({"from": "b", "to": "world", "time_us": 0})
        code, body = c.signed("GET", f"/v1/query?{q}")
        t = body["transform"]["translation"]
        m = body["transform"]["matrix"]
        expect_RT = [[0, 0, 1], [-1, 0, 0], [0, -1, 0]]
        matrix_ok = all(
            approx(m[i][j], expect_RT[i][j], 1e-7)
            for i in range(3) for j in range(3)
        )
        check("HTTP chain inverse analytic t=(0,1,2)",
              all(approx(t[i], v, 1e-7)
                  for i, v in enumerate((0, 1, 2))) and matrix_ok,
              f"t={t}")

        # ---- cycle / multi-parent / quaternion validation over HTTP ----
        code, body = c.signed("POST", "/v1/static", {
            "parent": "b", "child": "world",
            "transform": {"translation": [0, 0, 0],
                          "rotation": {"w": 1, "x": 0, "y": 0, "z": 0}}})
        check("cycle rejected 409 CYCLE_DETECTED",
              code == 409 and body["error"]["code"] == "CYCLE_DETECTED",
              f"{code} {body}")

        code, body = c.signed("POST", "/v1/static", {
            "parent": "x", "child": "a",
            "transform": {"translation": [0, 0, 0],
                          "rotation": {"w": 1, "x": 0, "y": 0, "z": 0}}})
        check("multi-parent rejected 409", code == 409, str(body))

        code, body = c.signed("POST", "/v1/static", {
            "parent": "u", "child": "v",
            "transform": {"translation": [0, 0, 0],
                          "rotation": {"w": 0.5, "x": 0.5, "y": 0, "z": 0}}})
        check("invalid quaternion rejected 422",
              code == 422 and
              body["error"]["code"] == "INVALID_QUATERNION", str(body))

        # ---- dynamic samples, interpolation, no extrapolation ----
        samples = {
            "parent": "b", "child": "tip",
            "samples": [
                {"stamp_us": 0,
                 "transform": {"translation": [0, 0, 0],
                               "rotation": {"w": 1, "x": 0, "y": 0, "z": 0}}},
                {"stamp_us": 10000,
                 "transform": {"translation": [10, 0, 0],
                               "rotation": q_axis_angle((0, 0, 1), math.pi / 2)}},
            ],
        }
        code, body = c.signed("POST", "/v1/samples", samples)
        check("add dynamic batch", code == 200 and
              body["edge"]["total_samples"] == 2, str(body))

        # async arrival: older sample added after newer ones
        code, body = c.signed("POST", "/v1/samples", {
            "parent": "b", "child": "tip",
            "samples": [
                {"stamp_us": 5000,
                 "transform": {"translation": [5, 0, 0],
                               "rotation": q_axis_angle(
                                   (0, 0, 1), math.pi / 4)}},
            ],
        })
        # stamp 5000 already implied? No: only 0 and 10000 exist -> inserted
        check("async late sample inserted", code == 200, str(body))

        # duplicate timestamp rejected
        code, body = c.signed("POST", "/v1/samples", {
            "parent": "b", "child": "tip",
            "samples": [
                {"stamp_us": 5000,
                 "transform": {"translation": [9, 0, 0],
                               "rotation": {"w": 1, "x": 0, "y": 0, "z": 0}}},
            ],
        })
        check("duplicate timestamp 409",
              code == 409 and
              body["error"]["code"] == "DUPLICATE_TIMESTAMP", str(body))

        q = urllib.parse.urlencode({"from": "b", "to": "tip", "time_us": 2500})
        code, body = c.signed("GET", f"/v1/query?{q}")
        t = body["transform"]["translation"]
        qq = body["transform"]["quaternion"]
        expected = q_axis_angle((0, 0, 1), math.pi / 8)
        quat_ok = (
            approx(qq["w"], expected["w"], 1e-7) and
            approx(abs(qq["z"]), abs(expected["z"]), 1e-7)
        )
        used = body["used_samples"][0]
        check("dynamic interpolation t=(2.5,0,0) Rz22.5",
              code == 200 and approx(t[0], 2.5, 1e-7) and quat_ok and
              used["interpolated"] and used["sample_a"] == 1 and
              used["sample_b"] == 3,  # ids: 0->1, 10000->2, 5000 inserted 3
              f"{t} {qq} {used}")

        q = urllib.parse.urlencode({"from": "b", "to": "tip", "time_us": 10001})
        code, body = c.signed("GET", f"/v1/query?{q}")
        check("extrapolation rejected 422 OUT_OF_RANGE",
              code == 422 and body["error"]["code"] == "OUT_OF_RANGE",
              str(body))

        # exact-time at last sample: not interpolated
        q = urllib.parse.urlencode({"from": "b", "to": "tip", "time_us": 10000})
        code, body = c.signed("GET", f"/v1/query?{q}")
        check("exact last sample not interpolated",
              code == 200 and not body["used_samples"][0]["interpolated"],
              str(body))

        # ---- latest mode across asynchronous edges: spread reported ----
        code, _ = c.signed("POST", "/v1/samples", {
            "parent": "tip", "child": "gripper",
            "samples": [
                {"stamp_us": 7000,
                 "transform": {"translation": [0, 0, 3],
                               "rotation": {"w": 1, "x": 0, "y": 0, "z": 0}}},
            ],
        })
        check("add second dynamic edge", code == 200)
        q = urllib.parse.urlencode({"from": "b", "to": "gripper"})
        code, body = c.signed("GET", f"/v1/query?latest=1&{q}")
        check("latest mode spreads newest samples, time_error=3000",
              code == 200 and body["mode"] == "latest" and
              body["time_error_us"] == 3000 and len(body["used_samples"]) == 2,
              str(body)[:300])

        # ---- disconnected & missing ----
        code, _ = c.signed("POST", "/v1/static", {
            "parent": "other_root", "child": "other_leaf",
            "transform": {"translation": [0, 0, 0],
                          "rotation": {"w": 1, "x": 0, "y": 0, "z": 0}}})
        q = urllib.parse.urlencode({"from": "a", "to": "other_leaf",
                                    "time_us": 0})
        code, body = c.signed("GET", f"/v1/query?{q}")
        check("disconnected frames 422", code == 422 and
              body["error"]["code"] == "DISCONNECTED", str(body))
        q = urllib.parse.urlencode({"from": "a", "to": "ghost", "time_us": 0})
        code, body = c.signed("GET", f"/v1/query?{q}")
        check("missing frame 404", code == 404 and
              body["error"]["code"] == "FRAME_NOT_FOUND", str(body))

        # ---- near-180 degree rotation chain over HTTP ----
        code, _ = c.signed("POST", "/v1/static", {
            "parent": "gripper", "child": "flip",
            "transform": {
                "translation": [0, 0, 0],
                "rotation": q_axis_angle((1, 1, 0), math.radians(179.999)),
            },
        })
        q = urllib.parse.urlencode({"from": "flip", "to": "gripper"})
        code, body = c.signed("GET", f"/v1/query?latest=1&{q}")
        qq = body["transform"]["quaternion"]
        norm = math.sqrt(sum(qq[k] * qq[k] for k in "wxyz"))
        # inverse of 179.999 about (1,1,0): angle -179.999, finite & unit
        check("near-180 inversion finite and unit norm",
              code == 200 and approx(norm, 1.0, 1e-9) and
              all(math.isfinite(qq[k]) for k in "wxyz"), str(body)[:200])

        # ---- tree listing ----
        code, body = c.signed("GET", "/v1/tree")
        names = {f["frame"] for f in body["frames"]}
        check("tree lists frames with edge types",
              code == 200 and {"world", "a", "b", "tip", "gripper",
                               "flip"} <= names, str(body)[:300])

        # ---- malformed HTTP / bad JSON / unknown route ----
        with socket.create_connection((HOST, port), timeout=5) as s:
            s.sendall(b"GARBAGE NOT HTTP\r\n\r\n")
            data = s.recv(256)
        check("malformed request -> 400", data.startswith(b"HTTP/1.1 400"),
              data[:64].decode(errors="replace"))

        ts = str(int(time.time()))
        bad_body = b"{not json"
        mac = hmac.new(KEY.encode(),
                       f"POST\n/v1/static\n{ts}\n".encode() + bad_body,
                       hashlib.sha256)
        code, body = c._request(
            "POST", "/v1/static", bad_body,
            {"Content-Type": "application/json",
             "X-TF-Timestamp": ts, "X-TF-Signature": mac.hexdigest()})
        check("bad JSON -> 400 BAD_JSON",
              code == 400 and body["error"]["code"] == "BAD_JSON", str(body))

        code, body = c.signed("GET", "/v1/nope")
        check("unknown route 404", code == 404, str(body))

    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    print(f"\n{len(SUCCESSES)} passed, {len(FAILURES)} failed")
    if FAILURES:
        for name, detail in FAILURES:
            print(f"  FAILED: {name} {detail}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
