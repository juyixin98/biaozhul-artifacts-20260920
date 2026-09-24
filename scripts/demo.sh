#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Manual demo: two real OS processes exchange the example input file through
# a 4-slot shared-memory ring, then the admin tool inspects and removes it.
set -euo pipefail
cd "$(dirname "$0")/.."

NAME="demo_$$"
CAP=4
MAX=4096

echo "== 1. environment check (lock-free cross-process atomics)"
./shmring-ctl check

echo "== 2. create /dev/shm/shmring_${NAME} (capacity=${CAP}, max_msg=${MAX})"
./shmring-ctl create "$NAME" --capacity "$CAP" --max-msg "$MAX"

echo "== 3. start consumer process (waits for up to 10 s)"
./shmring-consumer "$NAME" --capacity "$CAP" --max-msg "$MAX" \
    --count 6 --timeout 10000 > /tmp/shmring_demo.out 2>/tmp/shmring_demo.err &
CONS_PID=$!
sleep 0.3

echo "== 4. start producer process, feed examples/input.txt line by line"
head -6 examples/input.txt | ./shmring-producer "$NAME" \
    --capacity "$CAP" --max-msg "$MAX" --stdin --timeout 10000 \
    --flush

wait "$CONS_PID" || true
echo "== 5. consumer output:"
cat /tmp/shmring_demo.out
cat /tmp/shmring_demo.err

echo "== 6. queue state and integrity audit:"
./shmring-ctl info "$NAME"
./shmring-ctl doctor "$NAME"

echo "== 7. cleanup"
./shmring-ctl destroy "$NAME"
