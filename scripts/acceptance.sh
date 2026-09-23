#!/usr/bin/env bash
# Local acceptance runner: starts uvicorn against the local "epe" database,
# runs the live HTTP demo (including concurrent merge), then demonstrates
# crash-between-transactions recovery with the injected fault.
set -euo pipefail

cd "$(dirname "$0")/.."
PY=.venv/bin/python
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"
export DATABASE_URL="${DATABASE_URL:-postgresql://epe:epe_dev_pw@localhost:5432/epe}"
BASE="http://127.0.0.1:8000"

echo "== resetting dev database schema/data =="
PSQL=(psql "$DATABASE_URL" -v ON_ERROR_STOP=1)
"${PSQL[@]}" -c "
DROP TABLE IF EXISTS penalties, evidence, votes, stake_snapshots, validators, chains CASCADE;
"

echo "== starting server =="
unset CRASH_AFTER_EVIDENCE
$PY -m uvicorn app.main:app --host 127.0.0.1 --port 8000 </dev/null >/tmp/epe_uvicorn.log 2>&1 &
SERVER_PID=$!
trap 'kill $SERVER_PID 2>/dev/null || true' EXIT

for i in $(seq 1 50); do
  curl -sf "$BASE/health" >/dev/null 2>&1 && break
  sleep 0.2
done
curl -sf "$BASE/health" && echo

echo
echo "########## LIVE DEMO (reverse order, boundaries, security, concurrency) ##########"
EPE_BASE_URL="$BASE" $PY scripts/live_demo.py

echo
echo "########## CRASH RECOVERY DEMO ##########"
echo "-- restarting server with CRASH_AFTER_EVIDENCE=1 (penalty step will crash)"
kill "$SERVER_PID"; wait "$SERVER_PID" 2>/dev/null || true
CRASH_AFTER_EVIDENCE=1 $PY -m uvicorn app.main:app --host 127.0.0.1 --port 8000 \
  </dev/null >/tmp/epe_uvicorn_crash.log 2>&1 &
SERVER_PID=$!
for i in $(seq 1 50); do
  curl -sf "$BASE/health" >/dev/null 2>&1 && break
  sleep 0.2
done

$PY - <<'PYEOF'
import httpx
from app.testsign import make_vote, new_validator

c = httpx.Client(base_url="http://127.0.0.1:8000", timeout=10, trust_env=False)
v = new_validator()
c.post("/chains", json={"chain_id": "crash-chain"})
r = c.post("/validators", json={"chain_id": "crash-chain", "public_key": v["public_key"]})
addr = r.json()["validator_address"]
c.post("/snapshots", json={"chain_id": "crash-chain", "validator_address": addr,
                           "epoch": 0, "voting_power": 500_000})
for bh in ("aa" * 32, "bb" * 32):
    r = c.post("/votes", json=make_vote(v, chain_id="crash-chain", height=6,
                                        round=0, vote_type="prevote", block_hash=bh))
print("last ingest response (expected 500 crash):", r.status_code)
ev = c.get("/evidence", params={"chain_id": "crash-chain"}).json()["evidence"]
detail = c.get(f"/evidence/{ev[0]['evidence_id']}").json()
assert len(ev) == 1 and ev[0]["status"] == "DETECTED" and detail["penalty"] is None, \
    "unexpected post-crash state"
print("OK: evidence committed (DETECTED), no penalty yet")
PYEOF

echo "-- restarting server normally; startup recovery must finalize the penalty"
kill "$SERVER_PID"; wait "$SERVER_PID" 2>/dev/null || true
unset CRASH_AFTER_EVIDENCE
$PY -m uvicorn app.main:app --host 127.0.0.1 --port 8000 </dev/null >/tmp/epe_uvicorn_recov.log 2>&1 &
SERVER_PID=$!
for i in $(seq 1 50); do
  curl -sf "$BASE/health" >/dev/null 2>&1 && break
  sleep 0.2
done

$PY - <<'PYEOF'
import httpx
c = httpx.Client(base_url="http://127.0.0.1:8000", timeout=10, trust_env=False)
ev = c.get("/evidence", params={"chain_id": "crash-chain"}).json()["evidence"][0]
assert ev["status"] == "PENALIZED", ev
detail = c.get(f"/evidence/{ev['evidence_id']}").json()
assert detail["penalty"]["slashed_power"] == 5_000
print("OK after restart:", ev["status"], "slashed", detail["penalty"]["slashed_power"],
      "from snapshot", detail["penalty"]["snapshot_id"])
# second recovery changes nothing
r = c.post("/recover").json()
assert r["recovered"] == 0
print("OK: repeat recovery is a no-op")
PYEOF

echo
echo "ALL ACCEPTANCE FLOWS PASSED"
