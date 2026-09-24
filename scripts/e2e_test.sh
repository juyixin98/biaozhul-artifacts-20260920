#!/usr/bin/env bash
# e2e_test.sh — start the real server, exercise the HTTP API with curl, and
# independently verify the returned trajectory in Python (numeric, not trusting
# the service's own verification report).
set -euo pipefail

SERVER_BIN="${1:?usage: e2e_test.sh <server-binary> <examples-dir>}"
EXAMPLES="${2:?usage: e2e_test.sh <server-binary> <examples-dir>}"
PORT="${JTP_TEST_PORT:-18099}"
BASE="http://127.0.0.1:${PORT}"

TMPDIR="$(mktemp -d)"
trap 'kill "${SERVER_PID:-0}" 2>/dev/null || true; rm -rf "$TMPDIR"' EXIT

echo "[e2e] starting server: $SERVER_BIN --port $PORT"
"$SERVER_BIN" --host 127.0.0.1 --port "$PORT" >"$TMPDIR/server.log" 2>&1 &
SERVER_PID=$!

# Wait for the listening socket (real readiness probe).
for i in $(seq 1 100); do
  if curl -fsS "$BASE/health" >/dev/null 2>&1; then break; fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "[e2e] server died early"; cat "$TMPDIR/server.log"; exit 1
  fi
  sleep 0.05
  [[ $i -eq 100 ]] && { echo "[e2e] server not ready"; cat "$TMPDIR/server.log"; exit 1; }
done

fail() { echo "FAIL: $*" >&2; exit 1; }

# ------------------------------------------------------------- 1. health
curl -fsS "$BASE/health" | jq -e '(.ok == true) and (.service | type == "string")' >/dev/null
echo "[e2e] GET /health ok"

# ------------------------------------------------------------- 2. routing
code=$(curl -sS -o /dev/null -w '%{http_code}' "$BASE/nope")
[[ "$code" == "404" ]] || fail "expected 404, got $code"
code=$(curl -sS -o /dev/null -w '%{http_code}' -X GET "$BASE/parameterize")
[[ "$code" == "405" ]] || fail "expected 405, got $code"
code=$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$BASE/parameterize" -d 'not-json')
[[ "$code" == "400" ]] || fail "expected 400 on malformed JSON, got $code"
echo "[e2e] 404/405/400 routing ok"

# ------------------------------------------------------------- 3. infeasible
code=$(curl -sS -o "$TMPDIR/inf.json" -w '%{http_code}' -X POST \
  -H 'Content-Type: application/json' \
  --data-binary @"$EXAMPLES/request_infeasible_short.json" \
  "$BASE/parameterize")
[[ "$code" == "422" ]] || fail "expected 422 infeasible, got $code"
jq -e '.ok == false and .error.code == "INFEASIBLE_BOUNDARY"' "$TMPDIR/inf.json" >/dev/null
echo "[e2e] infeasible boundary -> 422 INFEASIBLE_BOUNDARY"

# ------------------------------------------------------------- 4. main case
REQ="$EXAMPLES/request_multiaxis.json"
RESP="$TMPDIR/resp.json"
RAW="$TMPDIR/raw.json"
cp "$REQ" "$RAW"
code=$(curl -sS -o "$RESP" -w '%{http_code}' -X POST \
  -H 'Content-Type: application/json' --data-binary @"$RAW" \
  "$BASE/parameterize")
[[ "$code" == "200" ]] || { echo "body:"; cat "$RESP"; fail "expected 200, got $code"; }

# request_sha256 must equal the independently computed digest of exact bytes
want_sum="$(sha256sum "$RAW" | awk '{print $1}')"
got_sum="$(jq -r '.request_sha256' "$RESP")"
[[ "$want_sum" == "$got_sum" ]] || fail "sha256 mismatch: $want_sum vs $got_sum"
echo "[e2e] request_sha256 matches sha256sum ($got_sum)"

jq -e '.ok == true and .verification.passed == true' "$RESP" >/dev/null
echo "[e2e] service verification report passed"

# Bottlenecks must be reported for every segment
jq -e '(.segments | length) == ((.waypoints | length) - 1)' "$RESP" >/dev/null
jq -e '[.segments[] | select(.zero_length == false)] |
       all(.velocity_bottleneck_joint >= 0 and .acceleration_bottleneck_joint >= 0
           and (.binding_regime == "velocity" or .binding_regime == "acceleration"))' \
       "$RESP" >/dev/null
echo "[e2e] per-segment bottleneck joints + regimes reported"

# Show which joint limits actually bind, on which segment interval
echo "[e2e] limiting joints/intervals:"
python3 - "$RESP" <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))
ts = [w["time"] for w in r["waypoints"]]
for s in r["segments"]:
    if s["zero_length"]:
        print(f"  segment {s['index']}: dwell (duplicate point), "
              f"t=[{ts[s['index']]:.6f},{ts[s['index']+1]:.6f}]")
        continue
    print(f"  segment {s['index']}: t=[{ts[s['index']]:.6f},"
          f"{ts[s['index']+1]:.6f}] duration={s['duration']:.6f}s "
          f"regime={s['binding_regime']} "
          f"v-bottleneck=joint{s['velocity_bottleneck_joint']} "
          f"a-bottleneck=joint{s['acceleration_bottleneck_joint']}")
PY

# ------------------------------------------------------------- 5. independent
# Python independently reconstructs q(t) from the reported waypoint timing is
# not possible without per-segment laws; instead verify the STRICT invariants
# the API promises: strictly increasing times, states consistency, and limits.
python3 - "$RESP" "$REQ" <<'PY'
import json, math, sys
resp = json.load(open(sys.argv[1]))
req  = json.load(open(sys.argv[2]))
vmax = req["velocity_limits"]; amax = req["acceleration_limits"]
wp   = resp["waypoints"]; segs = resp["segments"]
dof  = len(vmax)
tol_v = 1e-8 * max(1.0, max(vmax))
tol_a = 1e-8 * max(1.0, max(amax))

# strictly increasing time
for a, b in zip(wp, wp[1:]):
    assert b["time"] > a["time"], "time must be strictly increasing"

# positions equal inputs, state dimensions consistent
for w, q0 in zip(wp, req["waypoints"]):
    assert all(math.isclose(x, y, abs_tol=1e-11) for x, y in zip(w["position"], q0))
    assert len(w["velocity"]) == dof and len(w["acceleration"]) == dof

# endpoint boundary velocities are zero by default
assert all(abs(x) <= tol_v for x in wp[0]["velocity"])
assert all(abs(x) <= tol_v for x in wp[-1]["velocity"])

# independent forward kinematic plausibility: per segment the peak joint
# velocities cannot exceed vmax; report fields must satisfy path ceilings.
for s, p0, p1 in zip(segs, req["waypoints"][:-1], req["waypoints"][1:]):
    L = math.sqrt(sum((b-a)**2 for a, b in zip(p0, p1)))
    if L == 0.0:
        assert s["zero_length"] and s["duration"] == req["dwell_time"]
        continue
    us = [abs(b-a)/L for a, b in zip(p0, p1)]
    V = min(v/u for v, u in zip(vmax, us) if u > 1e-12)
    A = min(a/u for a, u in zip(amax, us) if u > 1e-12)
    assert math.isclose(s["path_speed_ceiling"], V, rel_tol=1e-10, abs_tol=1e-12)
    assert math.isclose(s["path_acceleration_ceiling"], A, rel_tol=1e-10, abs_tol=1e-12)
    assert s["peak_path_speed"] <= V * (1 + 1e-9)
    # duration must be at least the time-optimal double-integrator lower bound
    w_tri = math.sqrt(A*L)
    if w_tri < V:
        Tmin = 2*w_tri/A
        assert s["binding_regime"] == "acceleration"
    else:
        Tmin = V/A + L/V
        assert s["binding_regime"] == "velocity"
    assert s["duration"] >= Tmin*(1-1e-9), (s["duration"], Tmin)

# service's own dense samples must have seen no overshoot
assert resp["verification"]["max_velocity_violation"] <= tol_v
assert resp["verification"]["max_acceleration_violation"] <= tol_a
assert resp["verification"]["total_samples"] == \
       resp["verification"]["samples_per_segment"] * len(segs)
print("[e2e] independent invariants verified (timing, ceilings, optimality, dimensions)")
PY

# Independent dense re-sampling verifier (re-solves the whole law in Python).
python3 "$(dirname "$0")/independently_verify.py" "$REQ" "$RESP" --samples 800

# ------------------------------------------------------------- 6. duplicates
code=$(curl -sS -o "$TMPDIR/dup.json" -w '%{http_code}' -X POST \
  --data-binary @"$EXAMPLES/request_duplicate_points.json" \
  "$BASE/parameterize")
[[ "$code" == "200" ]] || { cat "$TMPDIR/dup.json"; fail "dup case $code"; }
jq -e '(.zero_length_segments == [0,2])
       and (.verification.passed == true)
       and ([.waypoints[].time] as $t
            | ($t == ($t | sort)) and (($t | length) == ($t | unique | length)))' \
   "$TMPDIR/dup.json" >/dev/null
python3 "$(dirname "$0")/independently_verify.py" \
  "$EXAMPLES/request_duplicate_points.json" "$TMPDIR/dup.json" --samples 300 \
  | sed 's/^/[e2e]   /'
echo "[e2e] duplicate points: dwell segments + strict time ordering ok"

# ------------------------------------------------------------- 7. boundary
code=$(curl -sS -o "$TMPDIR/bnd.json" -w '%{http_code}' -X POST \
  --data-binary @"$EXAMPLES/request_boundary_speeds.json" \
  "$BASE/parameterize")
[[ "$code" == "200" ]] || { cat "$TMPDIR/bnd.json"; fail "boundary case $code"; }
jq -e '.verification.passed == true
       and (.waypoints[0].velocity[0] | . == 1.0)
       and (.segments[0].duration | . == 5.5)' "$TMPDIR/bnd.json" >/dev/null
python3 "$(dirname "$0")/independently_verify.py" \
  "$EXAMPLES/request_boundary_speeds.json" "$TMPDIR/bnd.json" --samples 800 \
  | sed 's/^/[e2e]   /'
echo "[e2e] analytic boundary-speed case T=5.5s verified over HTTP"

# ------------------------------------------------------------- 8. keep-alive
# Two POSTs over ONE persistent TCP connection must both succeed, and the
# server must advertise keep-alive (exercises the in-tree HTTP/1.1 parser,
# including body-buffer hand-off between requests).
{
  printf 'POST /parameterize HTTP/1.1\r\nHost: x\r\nContent-Length: %d\r\n\r\n' \
    "$(wc -c < "$REQ")"
  cat "$REQ"
} > "$TMPDIR/req1.bin"
python3 - "$PORT" "$TMPDIR/req1.bin" "$REQ" <<'PY'
import socket, sys, json
port = int(sys.argv[1]); raw = open(sys.argv[2], "rb").read()
req2 = open(sys.argv[3]).read()
s = socket.create_connection(("127.0.0.1", port))
s.sendall(raw)
def read_one(sock):
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(65536)
        if not chunk: raise RuntimeError("closed")
        buf += chunk
    head, rest = buf.split(b"\r\n\r\n", 1)
    n = None
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            n = int(line.split(b":")[1])
    while len(rest) < n:
        rest += sock.recv(65536)
    return head, rest[:n], rest[n:]
h, b, leftover = read_one(s)
assert b'"ok":true' in b, b
# second request on SAME connection
msg = (f"POST /parameterize HTTP/1.1\r\nHost: x\r\nContent-Length: {len(req2)}\r\n\r\n"
       ).encode() + req2.encode()
s.sendall(msg)
h2, b2, _ = read_one(s)
assert b'"ok":true' in b2, b2
assert b"keep-alive" in h2.lower()
s.close()
print("[e2e] HTTP keep-alive: two requests on one connection ok")
PY

# ------------------------------------------------------------- 9. 413
# Body just over the 16 MiB server cap (a few waypoints but padded with
# ignorable whitespace, so parsing cost stays negligible).
big="$TMPDIR/big.json"
{
  printf '{"waypoints":[[0.0],[1.0]],"velocity_limits":[1.0],"acceleration_limits":[1.0],"pad":"'
  head -c 17000000 /dev/zero | tr '\0' ' '
  printf '"}'
} > "$big"
code=$(curl -sS -o /dev/null -w '%{http_code}' -X POST --data-binary @"$big" \
  "$BASE/parameterize")
[[ "$code" == "413" ]] || fail "expected 413 for oversized body, got $code"
echo "[e2e] oversized body -> 413"

echo "[e2e] ALL E2E CHECKS PASSED"
