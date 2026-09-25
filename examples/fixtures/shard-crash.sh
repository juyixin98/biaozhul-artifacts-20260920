#!/usr/bin/env bash
# shard-crash.sh — an executor that starts a test, emits a failing attempt,
# starts a retry, then DIES (nonzero exit) before the retry produces a result.
#
# The merge supervisor notices the nonzero exit and synthesizes
# shard_finished{outcome:"crashed"}. The in-flight retry must surface as
# incomplete — it must never silently pass.
set -euo pipefail
SHARD="${TR_SHARD_ID:-s-crash}"

emit() { printf '%s\n' "$1"; }

emit "{\"type\":\"shard_started\",\"shard_id\":\"${SHARD}\"}"
emit "{\"type\":\"attempt_started\",\"shard_id\":\"${SHARD}\",\"test_id\":\"billing.charge\",\"attempt_id\":\"billing.charge#1\",\"attempt_no\":1}"
emit "{\"type\":\"attempt_result\",\"shard_id\":\"${SHARD}\",\"test_id\":\"billing.charge\",\"attempt_id\":\"billing.charge#1\",\"attempt_no\":1,\"result\":\"failed\",\"reason\":\"assertion\",\"message\":\"expected 200 got 500\"}"
# retry begins but the executor crashes before its result is flushed
emit "{\"type\":\"attempt_started\",\"shard_id\":\"${SHARD}\",\"test_id\":\"billing.charge\",\"attempt_id\":\"billing.charge#2\",\"attempt_no\":2}"
echo "executor panic: lost connection to worker" >&2
exit 137
