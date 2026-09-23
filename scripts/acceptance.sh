#!/usr/bin/env bash
# End-to-end acceptance test for the ROS replay checkpoint service.
#
# It:
#   1. generates the demo bag
#   2. starts the FastAPI service (real DDS publishing enabled)
#   3. drives a session over HTTP: play, pause, variable speed, a mid-stream
#      seek (new generation) and a tail seek
#   4. saves a checkpoint, restarts the service, and resumes from the checkpoint
#   5. verifies source-change rejection (tampers the bag -> restore refused)
#   6. runs the live DDS subscriber verifier against a fresh real-time replay
#
# Requires a sourced ROS 2 (Jazzy):  source /opt/ros/jazzy/setup.bash
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PORT="${PORT:-8011}"
BASE="http://127.0.0.1:${PORT}"
export BAG_ROOT="$ROOT/bags"
export CHECKPOINT_DIR="$ROOT/state/checkpoints"
export ROS_ENABLED="${ROS_ENABLED:-true}"
export TOPIC_PREFIX="${TOPIC_PREFIX:-/replay}"
export PYTHONPATH="$ROOT/src:${PYTHONPATH:-}"

red()  { printf '\033[31m%s\033[0m\n' "$*"; }
grn()  { printf '\033[32m%s\033[0m\n' "$*"; }
info() { printf '\033[36m• %s\033[0m\n' "$*"; }
die()  { red "FAIL: $*"; exit 1; }

# Read a dotted/indexed path from JSON on stdin, e.g. jqf ['status']['next_seq']
jqf() { python3 -c "import sys,json;d=json.load(sys.stdin);print(d$1)"; }
state_is() { [ "$(curl -s "$BASE/sessions/$1/status" | jqf "['state']")" = "$2" ]; }
# wait_cond runs its command in a child bash; export functions and env it needs.
export -f jqf state_is
export BASE
wait_cond() { # usage: wait_cond <seconds> <shell-cmd...>
  local deadline=$(( $(date +%s) + $1 )); shift
  until "$@" >/dev/null 2>&1; do [ "$(date +%s)" -ge "$deadline" ] && return 1; sleep 0.2; done
}

info "generating demo bag (1s; /tick,/tock @10Hz, /events @20Hz)"
rm -rf "$BAG_ROOT/demo" "$BAG_ROOT/broken"
python3 scripts/generate_bag.py --uri "$BAG_ROOT/demo" --seconds 1 >/dev/null || die "bag generation"
cp -r "$BAG_ROOT/demo" "$BAG_ROOT/broken"
python3 - <<'PY'
import os
p="bags/broken/"+[f for f in os.listdir("bags/broken") if f.endswith(".mcap")][0]
sz=os.path.getsize(p)
with open(p,"r+b") as f:
    f.seek(sz//4); f.write(b"\xff"*(sz//2))
PY

info "starting service on :$PORT"
python3 -m uvicorn --app-dir "$ROOT/src" rosreplay.api:app --host 127.0.0.1 --port "$PORT" \
  >"$ROOT/state_server.log" 2>&1 &
SRV=$!
cleanup() { kill "$SRV" 2>/dev/null || true; }
trap cleanup EXIT
wait_cond 10 bash -c "curl -s $BASE/health | grep -q ok" || { cat "$ROOT/state_server.log"; die "server did not start"; }

info "bag info"
TOTAL=$(curl -s -X POST "$BASE/bags/info" -H 'Content-Type: application/json' -d '{"uri":"demo"}' | jqf "['message_count']")
[ "$TOTAL" = "43" ] || die "expected 43 messages, got $TOTAL"

# Corrupt bag -> 400
CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/bags/info" -H 'Content-Type: application/json' -d '{"uri":"broken"}')
[ "$CODE" = "400" ] || die "corrupt bag should return 400, got $CODE"
grn "corrupt bag rejected with 400"

info "create + pause + resume + rate + seek session"
# Start paused, then play at 1x for a short window and pause: guarantees a
# strictly partial replay (the bag spans ~1.1s) regardless of machine jitter.
SID=$(curl -s -X POST "$BASE/sessions" -H 'Content-Type: application/json' \
  -d '{"uri":"demo","rate":1,"play":false}' | jqf "['session_id']")
[ -n "$SID" ] || die "no session id"
curl -s -X POST "$BASE/sessions/$SID/resume" >/dev/null
sleep 0.25
curl -s -X POST "$BASE/sessions/$SID/pause" >/dev/null
N1=$(curl -s "$BASE/sessions/$SID/published" | jqf "['count']")
sleep 0.3
N2=$(curl -s "$BASE/sessions/$SID/published" | jqf "['count']")
[ "$N1" = "$N2" ] || die "paused session kept emitting ($N1 -> $N2)"
[ "$N1" -ge 1 ] && [ "$N1" -lt 43 ] || die "expected a partial replay when paused, got $N1"
grn "pause halts emission mid-stream ($N1/43 messages)"

curl -s -X POST "$BASE/sessions/$SID/rate" -H 'Content-Type: application/json' -d '{"rate":4000}' >/dev/null
curl -s -X POST "$BASE/sessions/$SID/resume" >/dev/null
wait_cond 10 bash -c "state_is $SID finished" || die "did not finish after fast-forward"
grn "variable speed + resume reached end"

# Generation guard on seek
info "seek creates a new generation; old queued messages do not leak"
# Start PAUSED, publish a bounded prefix, pause again, then jump forward and let
# only the new generation finish. This makes the no-leak check deterministic:
# the old generation provably has unemitted messages queued at jump time.
SID2=$(curl -s -X POST "$BASE/sessions" -H 'Content-Type: application/json' \
  -d '{"uri":"demo","rate":2,"play":true}' | jqf "['session_id']")
sleep 0.12
curl -s -X POST "$BASE/sessions/$SID2/pause" >/dev/null
PREFIX=$(curl -s "$BASE/sessions/$SID2/published" | jqf "['count']")
[ "$PREFIX" -ge 1 ] && [ "$PREFIX" -lt 40 ] || die "expected a small prefix (<40) before jump, got $PREFIX"
OLD_GEN=$(curl -s "$BASE/sessions/$SID2/status" | jqf "['generation']")
NEW_GEN=$(curl -s -X POST "$BASE/sessions/$SID2/seek" -H 'Content-Type: application/json' \
  -d '{"seq":40,"play":true}' | jqf "['generation']")
[ "$NEW_GEN" = "$((OLD_GEN+1))" ] || die "seek did not bump generation"
curl -s -X POST "$BASE/sessions/$SID2/rate" -H 'Content-Type: application/json' -d '{"rate":4000}' >/dev/null
wait_cond 5 bash -c "state_is $SID2 finished" || die "seek session did not finish"
OLDMAX=$(curl -s "$BASE/sessions/$SID2/published?generation=$OLD_GEN" \
  | python3 -c "import sys,json;r=json.load(sys.stdin)['records'];print(max((x['seq'] for x in r),default=-1))")
[ "$OLDMAX" -lt 40 ] || die "old generation leaked seq $OLDMAX after jump to 40"
NEWMIN=$(curl -s "$BASE/sessions/$SID2/published?generation=$NEW_GEN" \
  | python3 -c "import sys,json;r=json.load(sys.stdin)['records'];print(min((x['seq'] for x in r),default=-1))")
[ "$NEWMIN" -ge 40 ] || die "new generation started at $NEWMIN, expected >=40"
grn "generation guard holds (old max=$OLDMAX <40, new min=$NEWMIN)"

# Tail seek
info "seek to tail -> finished, no emission"
SID3=$(curl -s -X POST "$BASE/sessions" -H 'Content-Type: application/json' \
  -d '{"uri":"demo","play":false}' | jqf "['session_id']")
TAIL_STATE=$(curl -s -X POST "$BASE/sessions/$SID3/seek" -H 'Content-Type: application/json' \
  -d '{"seq":43,"play":false}' | jqf "['state']")
[ "$TAIL_STATE" = "finished" ] || die "tail seek state=$TAIL_STATE"
grn "tail seek yields finished with no emission"

# Checkpoint -> restart -> resume
info "checkpoint, restart service, resume"
SID4=$(curl -s -X POST "$BASE/sessions" -H 'Content-Type: application/json' \
  -d '{"uri":"demo","rate":1,"play":false}' | jqf "['session_id']")
curl -s -X POST "$BASE/sessions/$SID4/resume" >/dev/null
sleep 0.3
curl -s -X POST "$BASE/sessions/$SID4/pause" >/dev/null
NEXT=$(curl -s -X POST "$BASE/sessions/$SID4/checkpoints" -H 'Content-Type: application/json' \
  -d '{"checkpoint_id":"accept_cp"}' | jqf "['position']['next_seq']")
[ "$NEXT" -ge 3 ] && [ "$NEXT" -lt 43 ] || die "checkpoint position unexpected: next_seq=$NEXT"
kill "$SRV"; wait "$SRV" 2>/dev/null || true
python3 -m uvicorn --app-dir "$ROOT/src" rosreplay.api:app --host 127.0.0.1 --port "$PORT" \
  >"$ROOT/state_server2.log" 2>&1 &
SRV=$!
wait_cond 10 bash -c "curl -s $BASE/health | grep -q ok" || die "server did not restart"
RESTORED=$(curl -s -X POST "$BASE/checkpoints/restore" -H 'Content-Type: application/json' \
  -d '{"checkpoint_id":"accept_cp","play":false}')
RNEXT=$(printf '%s' "$RESTORED" | jqf "['status']['next_seq']")
[ "$RNEXT" = "$NEXT" ] || die "restored position $RNEXT != saved $NEXT"
grn "restart resume: position preserved (next_seq=$NEXT)"

# Source-change rejection
info "tamper bag -> restore must be refused"
echo '# tampered' >> "$BAG_ROOT/demo/metadata.yaml"
CODE=$(curl -s -o /tmp/cpresp -w '%{http_code}' -X POST "$BASE/checkpoints/restore" \
  -H 'Content-Type: application/json' -d '{"checkpoint_id":"accept_cp"}')
[ "$CODE" = "409" ] || die "tampered restore should be 409, got $CODE"
grep -q "source bag changed" /tmp/cpresp || die "unexpected tamper response: $(cat /tmp/cpresp)"
grn "source change refused with 409"

# Live DDS subscriber verification (regenerate clean bag first)
if [ "${RUN_DDS_VERIFY:-1}" = "1" ]; then
  info "live DDS subscriber verification"
  rm -rf "$BAG_ROOT/demo"
  python3 scripts/generate_bag.py --uri "$BAG_ROOT/demo" --seconds 1 >/dev/null
  # Create PAUSED so publishers exist, start the subscriber and give DDS
  # discovery time to connect, then trigger the real-time replay.
  SID5=$(curl -s -X POST "$BASE/sessions" -H 'Content-Type: application/json' \
    -d '{"uri":"demo","topics":["/tick","/tock"],"rate":1,"play":false}' | jqf "['session_id']")
  python3 scripts/verify_subscriber.py --duration 5 --expect tick=11,tock=11 \
    >/tmp/verify.log 2>&1 &
  VRF=$!
  sleep 2.0  # allow subscriptions to discover the paused session's publishers
  curl -s -X POST "$BASE/sessions/$SID5/resume" >/dev/null
  wait "$VRF" || { cat /tmp/verify.log; die "live subscriber verification failed"; }
  cat /tmp/verify.log | sed 's/^/  verifier: /'
fi

grn ""
grn "ALL ACCEPTANCE CHECKS PASSED"
