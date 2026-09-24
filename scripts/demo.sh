#!/usr/bin/env bash
# End-to-end demo for the 2PC simulator.
# Starts two participants and a coordinator, runs a happy commit, a vote-no
# abort, then a C2 coordinator crash (COMMIT durable, phase-2 not sent) and
# shows the restart recovering both participants to COMMIT.
#
# Usage: ./scripts/demo.sh [demo-data-dir]
set -u

BIN=${BIN:-./bin/tpc}
ROOT=${1:-./demo-data}
CPORT=19000
P1PORT=19101
P2PORT=19102
C=http://127.0.0.1:$CPORT
P1=http://127.0.0.1:$P1PORT
P2=http://127.0.0.1:$P2PORT

say() { printf '\n==== %s ====\n' "$1"; }
req() { curl -sS -m 5 -X "$1" "$2" ${3:+-H 'Content-Type: application/json' -d "$3"}; echo; }

mkdir -p "$ROOT"/{c1,p1,p2}

start_node() { # role dir port crash-env extra
  local role=$1 dir=$2 port=$3 extra=$5
  env ${4:-} "$BIN" $role -name "$role-$dir" -listen "127.0.0.1:$port" \
    -datadir "$ROOT/$dir" $extra >"$ROOT/$dir/stdout.log" 2>&1 &
  echo $!
}

wait_http() { # url
  for i in $(seq 1 50); do
    curl -sS -m 1 "$1/health" >/dev/null 2>&1 && return 0
    sleep 0.1
  done
  echo "timeout waiting for $1"; return 1
}

say "1. start participants + coordinator"
"$BIN" participant -name p1 -listen 127.0.0.1:$P1PORT -datadir "$ROOT/p1" >"$ROOT/p1/stdout.log" 2>&1 &
P1PID=$!
"$BIN" participant -name p2 -listen 127.0.0.1:$P2PORT -datadir "$ROOT/p2" >"$ROOT/p2/stdout.log" 2>&1 &
P2PID=$!
wait_http $P1
wait_http $P2
"$BIN" coordinator -name c1 -listen 127.0.0.1:$CPORT -datadir "$ROOT/c1" \
  -participants "p1=$P1,p2=$P2" >"$ROOT/c1/stdout.log" 2>&1 &
CPID=$!
wait_http $C
echo "all nodes up (c=$CPID p1=$P1PID p2=$P2PID)"

say "2. happy-path commit T1"
req POST $C/txn '{"txid":"T1","writes":[{"participant":"p1","key":"acc","value":"100"},{"participant":"p2","key":"log","value":"T1-ok"}]}'
req GET  $P1/kv/acc
req GET  $P2/kv/log

say "3. vote-no -> whole txn T2 aborts (p2 key blocked by prepared HOLDER)"
req POST $P2/prepare '{"txid":"HOLDER","writes":[{"key":"log","value":"held"}]}'
req POST $C/txn '{"txid":"T2","writes":[{"participant":"p1","key":"acc","value":"999"},{"participant":"p2","key":"log","value":"T2"}]}'
req GET  $P1/kv/acc
req GET  $P1/txn/T2
# Release the holder so later transactions can write p2's "log" key again.
req POST $P2/abort '{"txid":"HOLDER"}'

say "4. C2 crash: COMMIT durable on coordinator, phase-2 not delivered"
kill -9 $CPID 2>/dev/null
wait $CPID 2>/dev/null
TPC_CRASH=C2 "$BIN" coordinator -name c1 -listen 127.0.0.1:$CPORT -datadir "$ROOT/c1" \
  -participants "p1=$P1,p2=$P2" >"$ROOT/c1/crash.log" 2>&1 &
CPID=$!
wait_http $C
# Submit T3; coordinator dies (exit 42) right after fsync of COMMIT record.
curl -sS -m 5 -X POST $C/txn -H 'Content-Type: application/json' \
  -d '{"txid":"T3","writes":[{"participant":"p1","key":"acc","value":"300"},{"participant":"p2","key":"log","value":"T3-crash"}]}' \
  || echo "(connection dropped: coordinator crashed at C2)"
for i in $(seq 1 30); do kill -0 $CPID 2>/dev/null || break; sleep 0.1; done
if kill -0 $CPID 2>/dev/null; then echo "ERROR: coordinator did not crash"; else echo "coordinator exited at C2"; fi

say "5. while coordinator is dead: participants show PREPARED (blocked, NOT aborted)"
req GET $P1/txn/T3
req GET $P2/txn/T3
req POST $P1/recover '{}'

say "6. restart coordinator clean -> recovery redelivers COMMIT, both nodes commit"
"$BIN" coordinator -name c1 -listen 127.0.0.1:$CPORT -datadir "$ROOT/c1" \
  -participants "p1=$P1,p2=$P2" >>"$ROOT/c1/stdout.log" 2>&1 &
CPID=$!
wait_http $C
for i in $(seq 1 50); do
  s=$(curl -sS -m 2 $C/txn/T3 2>/dev/null)
  echo "$s" | grep -q COMMIT_COMPLETE && break
  sleep 0.2
done
req GET  $C/txn/T3
req GET  $P1/kv/acc
req GET  $P2/kv/log

say "coordinator log lines:"; cat "$ROOT/c1/stdout.log"

say "DONE (nodes left running; kill: kill $P1PID $P2PID $CPID)"
echo "p1=$P1PID p2=$P2PID c=$CPID" > "$ROOT/PIDS"
