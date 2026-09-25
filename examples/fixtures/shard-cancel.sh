#!/usr/bin/env bash
# shard-cancel.sh — a long-running shard that starts a test then waits. When
# the run is canceled, the supervisor sends SIGTERM; the test never produced
# a result and is reported as canceled, not passed.
set -euo pipefail
SHARD="${TR_SHARD_ID:-s-cancel}"

emit() { printf '%s\n' "$1"; }

emit "{\"type\":\"shard_started\",\"shard_id\":\"${SHARD}\"}"
emit "{\"type\":\"attempt_started\",\"shard_id\":\"${SHARD}\",\"test_id\":\"reports.export\",\"attempt_id\":\"reports.export#1\",\"attempt_no\":1}"
# no result — waiting on the test when cancel arrives
sleep 30 &
child=$!
trap 'kill "${child}" 2>/dev/null; exit 143' TERM
wait "${child}"
