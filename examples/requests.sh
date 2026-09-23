#!/usr/bin/env bash
# End-to-end request samples for the interval-lock server.
# Requires: a running server (default 127.0.0.1:3000), curl, and jq.
#
# Scenarios:
#   1) two-transaction deadlock cycle      (victim = max id)
#   2) three-transaction deadlock cycle    (victim = max id in the cycle)
#   3) adjacent / non-overlapping intervals (no blocking, no false positive)
#   4) S -> X lock upgrade (waits only while another S holder exists)
#
# Run:  bash examples/requests.sh
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:3000}"

say()  { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
post() { curl -s -o /tmp/lock_resp -w '%{http_code}' -X POST "$BASE$1" "${@:2}"; }
show() { printf 'HTTP %s  ' "$1"; cat /tmp/lock_resp | jq -c .; echo; }

say "reset state"
show "$(post /reset)"

# ---------------------------------------------------------------------------
say "1) TWO-transaction deadlock: T1<->T2, victim must be T2"
T1=$(post /txn | { read -r _; cat /tmp/lock_resp | jq .txn; })
T2=$(post /txn | { read -r _; cat /tmp/lock_resp | jq .txn; })
echo "txns: T1=$T1 T2=$T2"

show "$(post /lock -H 'content-type: application/json' \
  -d "{\"txn\":$T1,\"mode\":\"exclusive\",\"start\":0,\"end\":10}")"
show "$(post /lock -H 'content-type: application/json' \
  -d "{\"txn\":$T2,\"mode\":\"exclusive\",\"start\":20,\"end\":30}")"
show "$(post /lock -H 'content-type: application/json' \
  -d "{\"txn\":$T2,\"mode\":\"exclusive\",\"start\":0,\"end\":10}")"   # waiting
show "$(post /lock -H 'content-type: application/json' \
  -d "{\"txn\":$T1,\"mode\":\"exclusive\",\"start\":20,\"end\":30}")"  # deadlock victim=T2

say "state: wait-for edges must be empty"
curl -s "$BASE/state" | jq -c '{queue, wait_for, txns: [.txns[] | {id,state,held_count:(.held|length)}]}'
show "$(post "/txn/$T1/commit")"

# ---------------------------------------------------------------------------
say "2) THREE-transaction deadlock: 1->3->2->1, victim must be T3"
A=$(post /txn | { read -r _; jq .txn /tmp/lock_resp; })
B=$(post /txn | { read -r _; jq .txn /tmp/lock_resp; })
C=$(post /txn | { read -r _; jq .txn /tmp/lock_resp; })
echo "txns: A=$A B=$B C=$C"

show "$(post /lock -H 'content-type: application/json' -d "{\"txn\":$A,\"mode\":\"exclusive\",\"start\":0,\"end\":10}")"
show "$(post /lock -H 'content-type: application/json' -d "{\"txn\":$B,\"mode\":\"exclusive\",\"start\":10,\"end\":20}")"
show "$(post /lock -H 'content-type: application/json' -d "{\"txn\":$C,\"mode\":\"exclusive\",\"start\":20,\"end\":30}")"
show "$(post /lock -H 'content-type: application/json' -d "{\"txn\":$A,\"mode\":\"exclusive\",\"start\":20,\"end\":30}")" # A waits C
show "$(post /lock -H 'content-type: application/json' -d "{\"txn\":$C,\"mode\":\"exclusive\",\"start\":10,\"end\":20}")" # C waits B
show "$(post /lock -H 'content-type: application/json' -d "{\"txn\":$B,\"mode\":\"exclusive\",\"start\":0,\"end\":10}")"  # B waits A -> deadlock, victim C(T3)

say "state: C aborted with no locks; A granted [20,30); B still waiting for A"
curl -s "$BASE/state" | jq -c '.txns[] | {id,state,held:[.held[].interval],waiting:.waiting.interval}'
show "$(post "/txn/$A/commit")"   # B proceeds
show "$(post "/txn/$B/commit")"

# ---------------------------------------------------------------------------
say "3) ADJACENT intervals never overlap: all exclusive locks granted"
X=$(post /txn | { read -r _; jq .txn /tmp/lock_resp; })
Y=$(post /txn | { read -r _; jq .txn /tmp/lock_resp; })
show "$(post /lock -H 'content-type: application/json' -d "{\"txn\":$X,\"mode\":\"exclusive\",\"start\":0,\"end\":10}")"
show "$(post /lock -H 'content-type: application/json' -d "{\"txn\":$Y,\"mode\":\"exclusive\",\"start\":10,\"end\":20}")"
show "$(post /lock -H 'content-type: application/json' -d "{\"txn\":$X,\"mode\":\"exclusive\",\"start\":20,\"end\":30}")"
echo "wait_for (must be []):"; curl -s "$BASE/state" | jq -c .wait_for
show "$(post "/txn/$X/commit")"
show "$(post "/txn/$Y/commit")"

# ---------------------------------------------------------------------------
say "4) LOCK UPGRADE S->X"
P=$(post /txn | { read -r _; jq .txn /tmp/lock_resp; })
Q=$(post /txn | { read -r _; jq .txn /tmp/lock_resp; })
show "$(post /lock -H 'content-type: application/json' -d "{\"txn\":$P,\"mode\":\"shared\",\"start\":0,\"end\":10}")"
show "$(post /lock -H 'content-type: application/json' -d "{\"txn\":$Q,\"mode\":\"shared\",\"start\":0,\"end\":10}")"
show "$(post /lock -H 'content-type: application/json' -d "{\"txn\":$P,\"mode\":\"exclusive\",\"start\":0,\"end\":10}")" # waits Q
say "Q commits -> P's upgrade is granted"
show "$(post "/txn/$Q/commit")"
curl -s "$BASE/state" | jq -c '.txns[] | select(.id=='"$P"') | {id,state,held:[.held[].mode],waiting}'
show "$(post "/txn/$P/commit")"

echo
say "DONE"
