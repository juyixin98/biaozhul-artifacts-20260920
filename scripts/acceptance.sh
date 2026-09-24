#!/usr/bin/env bash
# End-to-end acceptance check: tests + a live server walkthrough of every
# required scenario (asymmetric delay, time jump, drift change, few samples,
# wrap/restart, signed versions, validity intervals, tamper detection).
set -euo pipefail

cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
source .venv/bin/activate

echo "=== 1/5  automated test suite ==="
python -m pytest -q

echo
echo "=== 2/5  regenerate example inputs ==="
python scripts/generate_examples.py >/dev/null

echo
echo "=== 3/5  analysis of every scenario (offline) ==="
for f in asymmetric rtt_spikes time_jump drift_change wrap restart unknown_modulus few; do
  status=$(curl -s --max-time 30 -X POST http://127.0.0.1:${PORT:-8000}/api/v1/calibrations/analyze \
      -H 'Content-Type: application/json' \
      --data-binary @"examples/$f.json" | python -c 'import sys,json; print(json.load(sys.stdin)["status"])')
  printf '  %-16s -> %s\n' "$f" "$status"
done

echo
echo "=== 4/5  publish -> signed version -> convert -> history ==="
PUB=$(curl -s --max-time 30 -X POST http://127.0.0.1:${PORT:-8000}/api/v1/calibrations/publish \
    -H 'Content-Type: application/json' --data-binary @examples/asymmetric.json)
echo "$PUB" | python -c '
import sys, json
env = json.load(sys.stdin)["signed_version"]
p = env["payload"]
assert env["alg"] == "Ed25519"
print("  published version:", p["version_id"], "status:", p["status"])
print("  signature:", env["signature"][:24], "...")
'
COUNTER=$(python -c 'import json; print(json.load(open("examples/asymmetric.json"))["samples"][-1]["counter"])')
curl -s --max-time 30 -X POST http://127.0.0.1:${PORT:-8000}/api/v1/convert \
    -H 'Content-Type: application/json' \
    -d "{\"device_id\":\"demo-asymmetric\",\"counter\":$COUNTER}" \
  | python -c '
import sys, json
o = json.load(sys.stdin)
lo, p, hi = o["host_time"]["lower"], o["host_time"]["point"], o["host_time"]["upper"]
assert lo <= p <= hi and hi > lo
print(f"  convert: {p:.6f} in [{lo:.6f}, {hi:.6f}]  width={(hi-lo)*1e3:.2f} ms")
'
curl -s --max-time 30 http://127.0.0.1:${PORT:-8000}/api/v1/devices/demo-asymmetric/history \
  | python -c 'import sys,json; print("  history rows:", len(json.load(sys.stdin)["conversions"]))'

echo
echo "=== 5/5  cryptographic verification (valid + tampered) ==="
curl -s --max-time 30 http://127.0.0.1:${PORT:-8000}/api/v1/devices/demo-asymmetric/versions \
  | PORT="${PORT:-8000}" python -c '
import json, subprocess, tempfile, os, sys
env = json.load(sys.stdin)["versions"][0]
base = "http://127.0.0.1:%s/api/v1/verify" % os.environ["PORT"]
def verify(e):
    r = subprocess.run(["curl","-s","-X","POST",
        base,"-H","Content-Type: application/json",
        "-d",json.dumps(e)], capture_output=True, text=True)
    return json.loads(r.stdout)["valid"]
assert verify(env) is True, "fresh envelope must verify"
bad = json.loads(json.dumps(env))
bad["payload"]["model"]["beta_point"] = 999.0
assert verify(bad) is False, "tampered envelope must fail"
print("  intact signature: valid; tampered model: rejected")
'

echo
echo "ALL ACCEPTANCE CHECKS PASSED"
