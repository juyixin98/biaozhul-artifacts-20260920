#!/usr/bin/env bash
# shard-retry.sh — a follow-up executor that retries the test the crashing
# shard lost. billing.charge attempt #1 failed on s-crash; this shard runs
# attempt #2 (a distinct attempt id) and it passes. Because the test-level
# rollup uses the LATEST attempt, the test ends passed even though #1 failed.
set -euo pipefail
SHARD="${TR_SHARD_ID:-s-retry}"

emit() { printf '%s\n' "$1"; }

emit "{\"type\":\"shard_started\",\"shard_id\":\"${SHARD}\"}"
emit "{\"type\":\"attempt_started\",\"shard_id\":\"${SHARD}\",\"test_id\":\"billing.charge\",\"attempt_id\":\"billing.charge#2\",\"attempt_no\":2}"
emit "{\"type\":\"attempt_result\",\"shard_id\":\"${SHARD}\",\"test_id\":\"billing.charge\",\"attempt_id\":\"billing.charge#2\",\"attempt_no\":2,\"result\":\"passed\"}"
emit "{\"type\":\"shard_finished\",\"shard_id\":\"${SHARD}\",\"outcome\":\"completed\",\"exit_code\":0}"
