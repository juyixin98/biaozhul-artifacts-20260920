#!/usr/bin/env bash
# hang.sh — blocks until killed; used to prove the timeout path cancels a
# runaway executor without losing already-recorded events. `exec` makes
# sleep the actual fixture process so signals reach it directly.
echo '{"event_id":"h1","seq_no":1,"type":"shard_started","shard":"a"}'
echo "hanging (simulated wedged executor)" >&2
exec sleep 30
