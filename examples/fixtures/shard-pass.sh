#!/usr/bin/env bash
# shard-pass.sh — a fixture executor shard where every planned test passes.
#
# Emits newline-delimited JSON events on stdout. The merge service only ever
# runs commands the user explicitly supplies (see examples/requests/*.json);
# this script is one such command.
set -euo pipefail
SHARD="${TR_SHARD_ID:-s-pass}"

emit() { printf '%s\n' "$1"; }

emit "{\"type\":\"shard_started\",\"shard_id\":\"${SHARD}\"}"
emit "{\"type\":\"attempt_started\",\"shard_id\":\"${SHARD}\",\"test_id\":\"auth.login\",\"attempt_id\":\"auth.login#1\",\"attempt_no\":1}"
emit "{\"type\":\"attempt_result\",\"shard_id\":\"${SHARD}\",\"test_id\":\"auth.login\",\"attempt_id\":\"auth.login#1\",\"attempt_no\":1,\"result\":\"passed\"}"
emit "{\"type\":\"attempt_started\",\"shard_id\":\"${SHARD}\",\"test_id\":\"auth.logout\",\"attempt_id\":\"auth.logout#1\",\"attempt_no\":1}"
emit "{\"type\":\"attempt_result\",\"shard_id\":\"${SHARD}\",\"test_id\":\"auth.logout\",\"attempt_id\":\"auth.logout#1\",\"attempt_no\":1,\"result\":\"passed\"}"
emit "{\"type\":\"shard_finished\",\"shard_id\":\"${SHARD}\",\"outcome\":\"completed\",\"exit_code\":0}"
