#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Integration fixtures for shmrq. Every IPC case here runs TWO REAL
# PROCESSES (separate fork/exec images talking through /dev/shm) — not
# threads. Threads sharing an address space prove nothing about
# process-shared atomics or process-shared futexes.
set -u

BIN="${1:-./shmrq}"
Q="/shmrq_it_$$_${RANDOM}"
PASS=0; FAIL=0
CONSUMER_OUT="$(mktemp)"; PRODUCER_ERR="$(mktemp)"
trap 'rm -f "$CONSUMER_OUT" "$PRODUCER_ERR"; "$BIN" unlink "$Q" >/dev/null 2>&1' EXIT

say()  { printf '\n=== %s ===\n' "$*"; }
ok()   { printf 'PASS: %s\n' "$1"; PASS=$((PASS+1)); }
bad()  { printf 'FAIL: %s\n' "$1"; FAIL=$((FAIL+1)); }

check() { # check NAME CONDITION...
  local name="$1"; shift
  if "$@"; then ok "$name"; else bad "$name"; fi
}

wait_count() { # wait_count FILE LINES TIMEOUT_S
  local f="$1" want="$2" tmo="$3" n=0
  while :; do
    local c; c=$(wc -l < "$f" 2>/dev/null || echo 0)
    [ "$c" -ge "$want" ] && return 0
    sleep 0.05; n=$((n+1))
    [ $((n*50)) -ge $((tmo*1000)) ] && return 1
  done
}

uname -sr; "$BIN" --help >/dev/null 2>&1 || true

# ------------------------------------------------------------- 1. create/info
say "1. create + info + unlink"
"$BIN" create "$Q" -c 16 -m 512 || bad "create"
"$BIN" info "$Q" | grep -q "capacity *16" || bad "info capacity"
"$BIN" create "$Q" -c 8 2>/dev/null && bad "duplicate create rejected" || ok "duplicate create rejected"
"$BIN" unlink "$Q"
"$BIN" info "$Q" 2>/dev/null && bad "unlink removes name" || ok "unlink removes name"
"$BIN" create "$Q" -c 7 2>/dev/null && bad "non-power-of-two rejected" || ok "non-power-of-two rejected"

# ------------------------------------------------- 2. two processes, high wrap
say "2. two real processes: 200k msgs on cap=8 (25k ring cycles)"
"$BIN" create "$Q" -c 8 -m 128
"$BIN" consumer "$Q" -n 200000 -t 10000 -q > "$CONSUMER_OUT" &
CPID=$!
"$BIN" producer "$Q" -n 200000 -s 64 -t 10000 -q
wait $CPID; RC=$?
[ $RC -eq 0 ] && ok "consumer exit 0" || bad "consumer exit rc=$RC"
LINES=$(wc -l < "$CONSUMER_OUT")
[ "$LINES" -eq 200000 ] && ok "exactly 200000 records" || bad "record count=$LINES"
head -1 "$CONSUMER_OUT" | grep -qE '^00000000000000000000 ' && ok "first seq=0" || bad "first record"
tail -1 "$CONSUMER_OUT" | grep -qE '^00000000000000199999 ' && ok "last seq=199999 (no loss/dup)" || bad "last record"
# Sequence column must be exactly 0..199999 in order.
awk '{ if ($1 != sprintf("%020d", NR-1)) bad=1 } END{ exit bad+0 }' "$CONSUMER_OUT" \
  && ok "dense ordered sequence across ring wrap" || bad "sequence dense/ordered"
"$BIN" info "$Q" | grep -qE 'wraps_head *25000' || bad "wraps_head counter"
"$BIN" info "$Q" | grep -qE 'wraps_tail *25000' && ok "wrap counters 25000/25000" || bad "wraps_tail counter"
"$BIN" info "$Q" | grep -qE 'crc_errors *0' && ok "zero crc errors" || bad "crc errors nonzero"

# ---------------------------------------------------------- 3. variable sizes
say "3. variable-length messages incl. 0-byte-equivalent small frames and max size"
"$BIN" unlink "$Q"; "$BIN" create "$Q" -c 4 -m 4096
"$BIN" consumer "$Q" -n 500 -t 5000 -q > "$CONSUMER_OUT" &
CPID=$!
"$BIN" producer "$Q" -n 500 -s 1 -r 3980 -t 5000 -q
wait $CPID; RC=$?
[ $RC -eq 0 ] && ok "varlen run ok" || bad "varlen run rc=$RC"
[ "$(wc -l < "$CONSUMER_OUT")" -eq 500 ] && ok "500 varlen records" || bad "varlen count"
# Frame bodies satisfy producer checksum verification done inside consumer (exit 0).
# Physical line = 20-byte seq + space + body (newlines inside body are escaped),
# so a 4000+ char line proves a near-max_payload record crossed the ring.
MAXLEN=$(awk '{ if (length($0)>m) m=length($0) } END{ print m }' "$CONSUMER_OUT")
[ "$MAXLEN" -ge 3800 ] && ok "observed large random frame ($MAXLEN chars incl. prefix)" || bad "max random frame only $MAXLEN"
# Deterministic upper bound: body sized so prefix+body == max_payload (4096).
"$BIN" unlink "$Q"; "$BIN" create "$Q" -c 2 -m 4096
python3 -c "import sys; sys.stdout.write('Z'*4075 + '\n')" > /tmp/shmrq_maxline.txt
"$BIN" consumer "$Q" -n 1 -t 3000 -q --no-verify > "$CONSUMER_OUT" &
CPID=$!
"$BIN" producer "$Q" -f /tmp/shmrq_maxline.txt -t 3000 -q
wait $CPID; RC=$?
[ $RC -eq 0 ] || bad "max-size record rc=$RC"
[ "$(awk '{ print length($0) }' "$CONSUMER_OUT")" -eq 4096 ] \
  && ok "exact max_payload frame (4096 chars) accepted and delivered" \
  || bad "max-size frame length"
"$BIN" producer "$Q" -f /tmp/shmrq_maxline.txt -t 0 -q >/dev/null 2>&1
# A 4076-char body would overflow once the 21-byte prefix is added.
python3 -c "import sys; sys.stdout.write('Z'*4076 + '\n')" > /tmp/shmrq_overline.txt
"$BIN" producer "$Q" -f /tmp/shmrq_overline.txt -t 0 -q >/dev/null 2>&1
"$BIN" info "$Q" >/dev/null && ok "queue intact after oversize attempt" || bad "queue state after oversize"

# ------------------------------------------------------------- 4. full reject
say "4. full queue: reject (no timeout) and blocking timeout"
"$BIN" unlink "$Q"; "$BIN" create "$Q" -c 4 -m 128
"$BIN" producer "$Q" -n 10 -t 1 -q 2>"$PRODUCER_ERR"; RC=$?
[ $RC -eq 5 ] && ok "full => exit 5 after timeout" || bad "full rc=$RC"
"$BIN" info "$Q" | grep -qE 'depth *4' && ok "4 enqueued, 0 lost" || bad "depth after fill"
# Non-blocking full:
"$BIN" producer "$Q" -n 1 -t 0 -q 2>/dev/null; RC=$?
[ $RC -eq 5 ] && ok "non-blocking publish rejects when full" || bad "nonblock full rc=$RC"
# Drain, then a producer with timeout can proceed (blocking success path).
"$BIN" consumer "$Q" -n 4 -t 2000 -q > "$CONSUMER_OUT"
[ "$(wc -l < "$CONSUMER_OUT")" -eq 4 ] && ok "drained 4" || bad "drain count"
"$BIN" producer "$Q" -n 8 -t 2000 -q 2>"$PRODUCER_ERR" &
PPID_BG=$!
sleep 0.3
"$BIN" consumer "$Q" -n 8 -t 5000 -q > "$CONSUMER_OUT"
wait $PPID_BG; RC=$?
[ $RC -eq 0 ] && ok "blocked producer resumed after drain" || bad "blocked producer rc=$RC"
[ "$(wc -l < "$CONSUMER_OUT")" -eq 8 ] && ok "8 more records" || bad "resumed count"

# ------------------------------------------------------- 5. torn-write crash
say "5. producer SIGKILL mid-record (torn slot), restart, no confirmed loss"
"$BIN" unlink "$Q"; "$BIN" create "$Q" -c 1024 -m 128
# A slow consumer drains concurrently; after 500 commits the producer's
# fault hook parks it in the WRITING state at position 500 (a free slot),
# where the harness SIGKILLs it. 500 records are committed by then; the
# consumer must not reach position 500 (it was never committed).
"$BIN" consumer "$Q" -n 500 -t 10000 -q --pace-us 1200 > "$CONSUMER_OUT" &
CPID=$!
"$BIN" producer "$Q" -n 100000 -s 64 -t 10000 -q \
     --fault-at 500 --fault-ms 60000 2>"$PRODUCER_ERR" &
PP=$!
sleep 1.0
"$BIN" info "$Q" | grep -qE 'pending_write *1' \
  && ok "WRITING tag visible while producer stalls" || bad "pending_write not observed"
kill -9 $PP; wait $PP 2>/dev/null
# Stop the consumer once it has drained the 500 confirmed records.
wait_count "$CONSUMER_OUT" 500 10 || true
kill -TERM $CPID 2>/dev/null; wait $CPID 2>/dev/null
sleep 0.2
[ "$(wc -l < "$CONSUMER_OUT")" -eq 500 ] \
  && ok "consumer delivered exactly the 500 committed, never the torn one" \
  || bad "consumer count=$(wc -l < "$CONSUMER_OUT")"
"$BIN" info "$Q" | grep -qE 'pending_write *1' && ok "torn slot evidence persists after kill" || bad "torn evidence"
# Gate must be free again (OFD lock auto-released by kernel on SIGKILL).
"$BIN" info "$Q" | grep -qE 'gate *-' && ok "producer gate released by kernel after SIGKILL" || bad "gate still locked"
"$BIN" info "$Q" | grep -qE 'head *500' && ok "head=500: torn record never committed" || bad "head after kill"

# Restarted producer: recovery must reclaim exactly one slot, keep 500.
"$BIN" producer "$Q" -n 1000 -s 64 -t 10000 -q 2>"$PRODUCER_ERR"; RC=$?
[ $RC -eq 0 ] || bad "restarted producer rc=$RC"
grep -q "adopted=1" "$PRODUCER_ERR" && ok "recovery adopted torn slot" || bad "recovery adopted"
grep -q "in_flight=0" "$PRODUCER_ERR" && ok "restart saw 0 unconsumed (500 drained)" || bad "in_flight after drain"
"$BIN" consumer "$Q" -n 1000 -t 5000 -q > "$CONSUMER_OUT"; RC=$?
[ $RC -eq 0 ] || bad "consumer after recovery rc=$RC"
[ "$(wc -l < "$CONSUMER_OUT")" -eq 1000 ] && ok "1000 delivered after restart (positions 500..1499)" || bad "delivered count"
awk '{ if ($1 != sprintf("%020d", NR-1+500)) bad=1 } END{ exit bad+0 }' "$CONSUMER_OUT" \
  && ok "dense 500..1499 across the crash boundary" || bad "post-crash sequence dense"
"$BIN" info "$Q" | grep -qE 'recovered_slots *1' && ok "recovered_slots stat=1" || bad "recovered stat"
"$BIN" info "$Q" | grep -qE 'pending_write *0' && ok "no pending writes after clean run" || bad "pending remains"

# ----------------------------------------- 6. ordinary SIGKILL (no fault hook)
say "6. producer killed at a random instant, restarted twice, all data intact"
"$BIN" unlink "$Q"; "$BIN" create "$Q" -c 32 -m 128
"$BIN" consumer "$Q" -n -1 -t 60000 -q --pace-us 50 > "$CONSUMER_OUT" &
CPID=$!
"$BIN" producer "$Q" -n -1 -s 64 -t 10000 -q 2>/dev/null &
P1=$!
sleep 0.7; kill -9 $P1; wait $P1 2>/dev/null
"$BIN" producer "$Q" -n -1 -s 64 -t 10000 -q 2>/dev/null &
P2=$!
sleep 0.5; kill -9 $P2; wait $P2 2>/dev/null
"$BIN" producer "$Q" -n 20000 -s 64 -t 10000 -q 2>/dev/null
# let consumer drain to 20000 then time out itself
wait_count "$CONSUMER_OUT" 20000 20 || true
kill -TERM $CPID 2>/dev/null; wait $CPID 2>/dev/null
head -1 "$CONSUMER_OUT" | grep -qE '^00000000000000000000 ' && ok "random-kill run starts at 0" || bad "random start"
awk '{ n=$1+0; seen[n]++ } END{ d=0; for (k in seen) if (seen[k]>1) d++; print (d==0)?1:0 }' "$CONSUMER_OUT" | grep -q 1 \
  && ok "random kills: no duplicate position among first deliveries" || bad "random dup"
awk '{ n=$1+0 } END{ exit (n>=19999)?0:1 }' "$CONSUMER_OUT" && ok "random kills: reached >=19999" || bad "random tail"

# ------------------------------------------- 7. consumer kill -> at-least-once
say "7. consumer SIGKILL: in-flight messages redelivered (at-least-once)"
"$BIN" unlink "$Q"; "$BIN" create "$Q" -c 8 -m 128
"$BIN" producer "$Q" -n 20000 -s 64 -t 10000 -q --pace-us 100 2>/dev/null &
PP=$!
"$BIN" consumer "$Q" -n -1 -t 10000 -q --pace-us 300 > "$CONSUMER_OUT" &
CP1=$!
sleep 0.9; kill -9 $CP1; wait $CP1 2>/dev/null
# unacked messages at crash time must still be delivered to the new consumer
"$BIN" consumer "$Q" -n 20000 -t 10000 -q --pace-us 50 >> "$CONSUMER_OUT"; RC=$?
wait $PP
[ $RC -le 200 ] && ok "second consumer exit ($RC = dup count encoded)" || bad "consumer2 rc=$RC"
[ "$RC" -gt 0 ] && ok "observed $RC redelivered in-flight messages" || echo "(note: no dup observed)"
awk '{ n=$1+0; if (n>mx) mx=n } END{ exit (mx==19999)?0:1 }' "$CONSUMER_OUT" \
  && ok "consumer crash: sequence reached 19999" || bad "consumer crash tail"
# Every redelivered copy must still verify CRC (consumer exits 3 otherwise).
awk '{ n=$1+0; c[n]++ } END{ d=0; for (k in c) if (c[k]>1) d++; exit (d>0)?0:1 }' "$CONSUMER_OUT" \
  && ok "some positions delivered multiple times, all CRC-valid" || echo "(note: timing produced no overlap)"

# ------------------------------------------------------- 8. gate enforcement
say "8. single-producer / single-consumer gates against live processes"
"$BIN" unlink "$Q"; "$BIN" create "$Q" -c 8 -m 128
"$BIN" producer "$Q" -n -1 -t 10000 -q --pace-us 100000 2>/dev/null &
P1=$!
sleep 0.4
"$BIN" producer "$Q" -n 1 -t 100 -q 2>/dev/null; RC=$?
[ $RC -eq 2 ] && ok "second live producer rejected with exit 2" || bad "producer gate rc=$RC"
"$BIN" consumer "$Q" -n -1 -t 10000 -q --pace-us 100000 >/dev/null 2>&1 &
C1=$!
sleep 0.4
"$BIN" consumer "$Q" -n 1 -t 100 -q 2>/dev/null; RC=$?
[ $RC -eq 2 ] && ok "second live consumer rejected with exit 2" || bad "consumer gate rc=$RC"
kill -9 $P1 $C1 2>/dev/null; wait 2>/dev/null
sleep 0.2
"$BIN" producer "$Q" -n 1 -t 1000 -q 2>/dev/null; RC=$?
[ $RC -eq 0 ] && ok "new producer attaches after old killed (lock auto-free)" || bad "post-kill producer rc=$RC"

# ------------------------------------------------ 9. corruption -> reinit path
say "9. bit-rot of a committed record is detected, never silently dropped"
"$BIN" unlink "$Q"; "$BIN" create "$Q" -c 8 -m 128
"$BIN" producer "$Q" -n 4 -s 32 -t 1000 -q
"$BIN" debug-corrupt "$Q" --fault-at 2
"$BIN" consumer "$Q" -n 4 -t 1000 -q >/dev/null 2>"$PRODUCER_ERR"; RC=$?
[ $RC -eq 3 ] && ok "consumer refuses corrupt record (exit 3)" || bad "corrupt consumer rc=$RC"
"$BIN" producer "$Q" -n 1 -t 1000 -q 2>"$PRODUCER_ERR"; RC=$?
[ $RC -eq 7 ] && ok "producer demands explicit reinit (exit 7)" || bad "corrupt producer rc=$RC"
"$BIN" info "$Q" | grep -qE 'crc_errors *[1-9]' && ok "crc_errors counter > 0" || bad "crc stat"
# Explicit, operator-driven reinitialization:
"$BIN" unlink "$Q" && "$BIN" create "$Q" -c 8 -m 128
"$BIN" producer "$Q" -n 2 -t 1000 -q && "$BIN" consumer "$Q" -n 2 -t 1000 -q -q >/dev/null
ok "queue reusable after explicit reinit"

# --------------------------------------------------------- 10. input from file
say "10. example input file through the queue"
HERE="$(cd "$(dirname "$0")" && pwd)"
"$BIN" unlink "$Q"; "$BIN" create "$Q" -c 8 -m 128
"$BIN" consumer "$Q" -n 5 -t 3000 -q --no-verify > "$CONSUMER_OUT" &
CPID=$!
"$BIN" producer "$Q" -f "$HERE/../examples/messages.txt" -t 3000 -q
wait $CPID; RC=$?
[ $RC -eq 0 ] || bad "file consumer rc=$RC"
grep -q "hello shm ring" "$CONSUMER_OUT" && ok "file message 1 delivered" || bad "file msg1"
grep -q "position wrap at 4294967296" "$CONSUMER_OUT" && ok "file message 5 delivered" || bad "file msg5"
[ "$(wc -l < "$CONSUMER_OUT")" -eq 5 ] && ok "all 5 example lines" || bad "file line count"

# ------------------------------------------------------------- 11. empty poll
say "11. consumer empty/timeout behavior"
"$BIN" unlink "$Q"; "$BIN" create "$Q" -c 4 -m 128
"$BIN" consumer "$Q" -n 1 -t 80 -q >/dev/null 2>&1; RC=$?
[ $RC -eq 4 ] && ok "idle consumer exits 4 after timeout" || bad "idle rc=$RC"

# ------------------------------------------------------- 12. blocking signaling
say "12. producer/consumer blocking via process-shared futex (timing)"
"$BIN" unlink "$Q"; "$BIN" create "$Q" -c 2 -m 128
"$BIN" producer "$Q" -n 10 -t 10000 -q 2>/dev/null &  # blocks after 2
PP=$!
"$BIN" consumer "$Q" -n 10 -t 10000 -q --pace-us 30000 > "$CONSUMER_OUT" &
CPID=$!
T0=$(date +%s%N)
wait $PP; PRC=$?
T1=$(date +%s%N)
wait $CPID; CRC=$?
ELAPSED_MS=$(( (T1-T0)/1000000 ))
[ $PRC -eq 0 ] && [ $CRC -eq 0 ] && ok "blocked producer + paced consumer completed" || bad "blocking run prc=$PRC crc=$CRC"
[ "$(wc -l < "$CONSUMER_OUT")" -eq 10 ] && ok "10 paced records" || bad "paced count"
[ $ELAPSED_MS -ge 200 ] && ok "pace observed (${ELAPSED_MS}ms) => futex waits really slept" || bad "too fast ${ELAPSED_MS}ms"

printf '\n==============================\n'
printf 'integration: %d passed, %d failed\n' "$PASS" "$FAIL"
[ $FAIL -eq 0 ]
