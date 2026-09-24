#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Manual demo: two real processes on /dev/shm.
#   ./examples/demo.sh            # produce+consume 10 messages
#   ./examples/demo.sh kill       # kill producer mid-write, restart it
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
BIN="$HERE/../build/shmrq"
[ -x "$BIN" ] || BIN="$(command -v shmrq)"
Q="${SHMRQ_DEMO_NAME:-/shmrq_demo}"

"$BIN" unlink "$Q" >/dev/null 2>&1 || true
"$BIN" create "$Q" -c 8 -m 4000
echo "--- queue created: $Q ---"
"$BIN" info "$Q" | sed 's/^/  /'

if [ "${1:-}" = "kill" ]; then
  echo "--- producer stalls mid-record at seq 3; killing it with SIGKILL ---"
  "$BIN" producer "$Q" -n 100 -s 800 -t 5000 \
        --fault-at 3 --fault-ms 60000 2>/tmp/shmrq_prod.err &
  PP=$!
  sleep 1
  echo "  info while stalled:"; "$BIN" info "$Q" | grep -E 'head|pending_write|gate' | sed 's/^/    /'
  kill -9 $PP; wait $PP 2>/dev/null
  sleep 0.2
  echo "  producer killed; restarting (recovery log on stderr):"
  "$BIN" consumer "$Q" -n 9 -t 8000 --no-verify > /tmp/shmrq_demo.out &
  CP=$!
  "$BIN" producer "$Q" -n 6 -s 800 -t 5000 2>&1 >/dev/null | sed 's/^/    /'
  wait $CP
  echo "--- consumer drained everything that was confirmed ---"
  sed 's/^/  <= /' /tmp/shmrq_demo.out
else
  "$BIN" consumer "$Q" -n 10 -t 5000 > /tmp/shmrq_demo.out &
  CP=$!
  "$BIN" producer "$Q" -n 10 -s 1 -r 200 -t 5000
  wait $CP
  echo "--- consumed ---"
  sed 's/^/  <= /' /tmp/shmrq_demo.out
fi
"$BIN" info "$Q" | sed 's/^/  /'
"$BIN" unlink "$Q" >/dev/null
