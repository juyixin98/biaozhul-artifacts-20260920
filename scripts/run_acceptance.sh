#!/usr/bin/env bash
#
# End-to-end acceptance run against a REAL uvicorn server.
#
# Exercises:
#   * normal time-ordered replay with stable seq numbers
#   * pause / resume / rate change (timestamps unchanged)
#   * seek to the end and back (new generations; old gens never publish)
#   * corrupt bag rejected (HTTP 422)
#   * checkpoint -> restart -> continue
#   * checkpoint restore rejected when the source file changes (HTTP 409)
#   * independent verification subscriber (NDJSON protocol invariants)
#
# Usage:
#   source /opt/ros/jazzy/setup.bash
#   ./scripts/run_acceptance.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PORT="${REPLAY_PORT:-8011}"
BASE="http://127.0.0.1:${PORT}"
WORK="$(mktemp -d)"
trap 'SERVER_PID=$(cat "$WORK/server.pid" 2>/dev/null || true); [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null || true; rm -rf "$WORK"' EXIT

export REPLAY_BAG_ROOTS="${ROOT}/examples:${WORK}"
export REPLAY_STATE_DIR="$WORK/state"
export REPLAY_HMAC_KEY="acceptance-hmac-secret-key-0123456789ab"
export REPLAY_TRANSPORT="loopback"
export NO_PROXY="127.0.0.1,localhost"
export no_proxy="127.0.0.1,localhost"

# bypass any ambient http(s) proxy for loopback curl
CURL=(curl -sS --noproxy '*')

echo "==> Generating a fresh acceptance bag"
python3 -m bagtools.bagmaker "$WORK/acc_bag" --topics /alpha /beta \
    --messages 30 --gap-ms 50 --same-time-groups 2
REPLAY_BAG_ROOTS="$WORK" python3 -m bagtools.bagmaker "$WORK/bad_bag" \
    --topics /alpha --messages 12 --corrupt garbage_storage --same-time-groups 0

echo "==> Starting server on $BASE"
python3 -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" \
    --log-level warning >"$WORK/server.log" 2>&1 &
echo $! > "$WORK/server.pid"

for _ in $(seq 1 60); do
    if "${CURL[@]}" "$BASE/health" >/dev/null 2>&1; then break; fi
    sleep 0.25
done
echo "    health: $("${CURL[@]}" "$BASE/health")"

echo "==> Corrupt bag must be rejected (422 corrupt_bag)"
code=$("${CURL[@]}" -o "$WORK/bad.json" -w '%{http_code}' -X POST "$BASE/sessions" \
    -H 'Content-Type: application/json' -d "{\"bag_uri\":\"$WORK/bad_bag\"}")
echo "    http=$code body=$(cat "$WORK/bad.json")"
[ "$code" = "422" ] || { echo "FAIL: expected 422"; exit 1; }
grep -q corrupt_bag "$WORK/bad.json" || { echo "FAIL: expected corrupt_bag error"; exit 1; }

echo "==> Create session (rate 40) and autoplay"
SID=$("${CURL[@]}" -X POST "$BASE/sessions" -H 'Content-Type: application/json' \
    -d "{\"bag_uri\":\"$WORK/acc_bag\",\"rate\":40}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["session_id"])')
echo "    session=$SID"

# Start paused so the verification subscriber is attached before anything is
# published; it will then observe generation 0 fully and every later seek.
"${CURL[@]}" -X POST "$BASE/sessions/$SID/seek" \
    -H 'Content-Type: application/json' -d '{"seq":1}' >/dev/null

# verification subscriber in the background (NDJSON invariants)
python3 scripts/verify_subscriber.py --mode ndjson --base-url "$BASE" \
    --session-id "$SID" --duration 6 --report "$WORK/verify.json" \
    >"$WORK/verify.log" 2>&1 &
VERIFY_PID=$!

sleep 0.6
echo "==> Play; seek mid-bag; take a mid-playback checkpoint"
# slow to 1x so the checkpoint is provably taken mid-stream
"${CURL[@]}" -X POST "$BASE/sessions/$SID/rate" -H 'Content-Type: application/json' -d '{"rate":1}' >/dev/null
"${CURL[@]}" -X POST "$BASE/sessions/$SID/play" >/dev/null
sleep 0.2
# seek mid-bag (new generation) while the subscriber is listening, at 1x
"${CURL[@]}" -X POST "$BASE/sessions/$SID/seek" -H 'Content-Type: application/json' \
    -d '{"seq":10,"play_after":true}' >/dev/null
sleep 0.35  # ~7 messages at the 50ms gap, leaving more to play after resume

echo "==> Take a mid-playback checkpoint (pauses)"
CP=$("${CURL[@]}" -X POST "$BASE/sessions/$SID/checkpoint" -H 'Content-Type: application/json' \
    -d '{"pause":true}')
CP_ID=$(echo "$CP" | python3 -c 'import sys,json;print(json.load(sys.stdin)["checkpoint_id"])')
CP_NEXT=$(echo "$CP" | python3 -c 'import sys,json;print(json.load(sys.stdin)["state"]["next_seq"])')
echo "    checkpoint=$CP_ID next_seq=$CP_NEXT"
[ "$CP_NEXT" -ge 11 ] && [ "$CP_NEXT" -le 28 ] || { echo "FAIL: checkpoint not mid-bag (next_seq=$CP_NEXT)"; exit 1; }

echo "==> Seek to end (ratio 1.0) then back to seq 1"
"${CURL[@]}" -X POST "$BASE/sessions/$SID/seek" -H 'Content-Type: application/json' \
    -d '{"ratio":1.0}' | python3 -c 'import sys,json;s=json.load(sys.stdin)["state"];assert s["next_seq"]==31,s;print("    end seek next_seq=31 OK")'
"${CURL[@]}" -X POST "$BASE/sessions/$SID/seek" -H 'Content-Type: application/json' \
    -d '{"seq":1,"play_after":true}' >/dev/null
sleep 0.8
wait "$VERIFY_PID" && echo "    verification subscriber: OK" || {
    echo "FAIL: verification subscriber"; cat "$WORK/verify.log"; exit 1; }

echo "==> Restore checkpoint into a NEW session and continue to the end"
REST=$("${CURL[@]}" -X POST "$BASE/restore" -H 'Content-Type: application/json' \
    -d "{\"checkpoint_id\":\"$CP_ID\",\"autoplay\":true}")
NEW_SID=$(echo "$REST" | python3 -c 'import sys,json;print(json.load(sys.stdin)["session_id"])')
R_NEXT=$(echo "$REST" | python3 -c 'import sys,json;print(json.load(sys.stdin)["state"]["next_seq"])')
[ "$R_NEXT" = "$CP_NEXT" ] || { echo "FAIL: restore position $R_NEXT != $CP_NEXT"; exit 1; }
echo "    restored session=$NEW_SID at next_seq=$R_NEXT"
for _ in $(seq 1 50); do
    fin=$("${CURL[@]}" "$BASE/sessions/$NEW_SID" | python3 -c 'import sys,json;print(json.load(sys.stdin)["state"]["finished"])')
    [ "$fin" = "True" ] && break; sleep 0.1
done
[ "$fin" = "True" ] || { echo "FAIL: restored session did not finish"; exit 1; }
echo "    restored session played to end OK"

echo "==> Mutate source file; restore must be rejected (409 source_changed)"
MCAP=$(find "$WORK/acc_bag" -name '*.mcap' | head -1)
printf '\x00\x00\x00\x00' >> "$MCAP"
code=$("${CURL[@]}" -o "$WORK/src.json" -w '%{http_code}' -X POST "$BASE/restore" \
    -H 'Content-Type: application/json' -d "{\"checkpoint_id\":\"$CP_ID\"}")
echo "    http=$code body=$(cat "$WORK/src.json")"
[ "$code" = "409" ] || { echo "FAIL: expected 409"; exit 1; }
grep -q source_changed "$WORK/src.json" || { echo "FAIL: expected source_changed"; exit 1; }

echo
echo "==> Unit / protocol / race test-suite"
python3 -m pytest -q

echo
echo "ALL ACCEPTANCE CHECKS PASSED"
