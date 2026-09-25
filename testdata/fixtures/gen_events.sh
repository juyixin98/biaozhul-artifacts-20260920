#!/usr/bin/env bash
# gen_events.sh — the only executable a user points at for demos.
#
# Usage: gen_events.sh <scenario>
#
# Prints newline-delimited JSON test events to stdout. It does not accept
# arbitrary commands: <scenario> selects one canned, auditable sequence.
# run_id is omitted so the service stamps the run the events are ingested
# into; seq_no values are fixed per event (executor-assigned canonical
# order), which is what makes reordering demonstrable.
#
#   scenario     what it demonstrates
#   ------------ -----------------------------------------------------------
#   happy        two shards, mixed passed/failed, clean run_finished
#   retry        one test fails on attempt 1, passes on retry attempt 2
#   crash        executor dies mid-run: events present, NO run_finished
#                (and the process exits non-zero)
#   shuffled     identical events to "happy" but printed out of order
#   duplicates   every "happy" event printed twice (restart re-stream)
#   late-write   newer attempt wins; a late result for the OLD attempt
#                arrives afterwards and must not overwrite the new outcome
#   cancelled    run cancelled while one test is still running
#   missing      one test starts but never finishes; a second declared test
#                is never observed — neither may be counted as passed
#   malformed    non-JSON / truncated lines interleaved with valid events
set -euo pipefail

scenario="${1:-happy}"

happy_lines() {
cat <<'JSONL'
{"event_id":"s1","seq_no":1,"type":"shard_started","shard":"shard-a"}
{"event_id":"a1s","seq_no":2,"type":"attempt_started","shard":"shard-a","test_id":"test-alpha","attempt_id":"a-1","attempt_no":1}
{"event_id":"a1f","seq_no":3,"type":"attempt_finished","shard":"shard-a","test_id":"test-alpha","attempt_id":"a-1","attempt_no":1,"status":"passed"}
{"event_id":"b1s","seq_no":4,"type":"attempt_started","shard":"shard-a","test_id":"test-bravo","attempt_id":"b-1","attempt_no":1}
{"event_id":"b1f","seq_no":5,"type":"attempt_finished","shard":"shard-a","test_id":"test-bravo","attempt_id":"b-1","attempt_no":1,"status":"failed","message":"assertion 42 != 7"}
{"event_id":"s1e","seq_no":6,"type":"shard_finished","shard":"shard-a"}
{"event_id":"s2","seq_no":7,"type":"shard_started","shard":"shard-b"}
{"event_id":"c1s","seq_no":8,"type":"attempt_started","shard":"shard-b","test_id":"test-charlie","attempt_id":"c-1","attempt_no":1}
{"event_id":"c1f","seq_no":9,"type":"attempt_finished","shard":"shard-b","test_id":"test-charlie","attempt_id":"c-1","attempt_no":1,"status":"passed"}
{"event_id":"s2e","seq_no":10,"type":"shard_finished","shard":"shard-b"}
{"event_id":"rf1","seq_no":11,"type":"run_finished","status":"failed"}
JSONL
}

case "$scenario" in
  happy)
    happy_lines
    ;;

  retry)
cat <<'JSONL'
{"event_id":"s1","seq_no":1,"type":"shard_started","shard":"shard-a"}
{"event_id":"r1s","seq_no":2,"type":"attempt_started","shard":"shard-a","test_id":"test-flaky","attempt_id":"f-1","attempt_no":1}
{"event_id":"r1f","seq_no":3,"type":"attempt_finished","shard":"shard-a","test_id":"test-flaky","attempt_id":"f-1","attempt_no":1,"status":"failed","message":"first try blew up"}
{"event_id":"r2s","seq_no":4,"type":"attempt_started","shard":"shard-a","test_id":"test-flaky","attempt_id":"f-2","attempt_no":2}
{"event_id":"r2f","seq_no":5,"type":"attempt_finished","shard":"shard-a","test_id":"test-flaky","attempt_id":"f-2","attempt_no":2,"status":"passed"}
{"event_id":"s1e","seq_no":6,"type":"shard_finished","shard":"shard-a"}
{"event_id":"rf1","seq_no":7,"type":"run_finished","status":"passed"}
JSONL
    ;;

  crash)
    # No run_finished, last attempt unfinished; process exits non-zero.
    cat <<'JSONL'
{"event_id":"s1","seq_no":1,"type":"shard_started","shard":"shard-a"}
{"event_id":"x1s","seq_no":2,"type":"attempt_started","shard":"shard-a","test_id":"test-alpha","attempt_id":"a-1","attempt_no":1}
{"event_id":"x1f","seq_no":3,"type":"attempt_finished","shard":"shard-a","test_id":"test-alpha","attempt_id":"a-1","attempt_no":1,"status":"passed"}
{"event_id":"x2s","seq_no":4,"type":"attempt_started","shard":"shard-a","test_id":"test-delta","attempt_id":"d-1","attempt_no":1}
JSONL
    echo "executor: shard-a crashed (simulated)" >&2
    exit 91
    ;;

  shuffled)
    # Same event ids AND seq_nos as happy, physical line order scrambled.
    cat <<'JSONL'
{"event_id":"c1f","seq_no":9,"type":"attempt_finished","shard":"shard-b","test_id":"test-charlie","attempt_id":"c-1","attempt_no":1,"status":"passed"}
{"event_id":"b1s","seq_no":4,"type":"attempt_started","shard":"shard-a","test_id":"test-bravo","attempt_id":"b-1","attempt_no":1}
{"event_id":"rf1","seq_no":11,"type":"run_finished","status":"failed"}
{"event_id":"s2","seq_no":7,"type":"shard_started","shard":"shard-b"}
{"event_id":"a1f","seq_no":3,"type":"attempt_finished","shard":"shard-a","test_id":"test-alpha","attempt_id":"a-1","attempt_no":1,"status":"passed"}
{"event_id":"s1e","seq_no":6,"type":"shard_finished","shard":"shard-a"}
{"event_id":"s1","seq_no":1,"type":"shard_started","shard":"shard-a"}
{"event_id":"c1s","seq_no":8,"type":"attempt_started","shard":"shard-b","test_id":"test-charlie","attempt_id":"c-1","attempt_no":1}
{"event_id":"s2e","seq_no":10,"type":"shard_finished","shard":"shard-b"}
{"event_id":"a1s","seq_no":2,"type":"attempt_started","shard":"shard-a","test_id":"test-alpha","attempt_id":"a-1","attempt_no":1}
{"event_id":"b1f","seq_no":5,"type":"attempt_finished","shard":"shard-a","test_id":"test-bravo","attempt_id":"b-1","attempt_no":1,"status":"failed","message":"assertion 42 != 7"}
JSONL
    ;;

  duplicates)
    # Identical lines twice (restart re-streamed the whole log).
    happy_lines
    happy_lines
    ;;

  late-write)
cat <<'JSONL'
{"event_id":"s1","seq_no":1,"type":"shard_started","shard":"shard-a"}
{"event_id":"l1s","seq_no":2,"type":"attempt_started","shard":"shard-a","test_id":"test-late","attempt_id":"t-1","attempt_no":1}
{"event_id":"l1f","seq_no":3,"type":"attempt_finished","shard":"shard-a","test_id":"test-late","attempt_id":"t-1","attempt_no":1,"status":"failed","message":"attempt 1 failed"}
{"event_id":"l2s","seq_no":4,"type":"attempt_started","shard":"shard-a","test_id":"test-late","attempt_id":"t-2","attempt_no":2}
{"event_id":"l2f","seq_no":5,"type":"attempt_finished","shard":"shard-a","test_id":"test-late","attempt_id":"t-2","attempt_no":2,"status":"passed"}
{"event_id":"l1-late","seq_no":6,"type":"attempt_finished","shard":"shard-a","test_id":"test-late","attempt_id":"t-1","attempt_no":1,"status":"cancelled","message":"late cancel for OLD attempt, must be ignored"}
{"event_id":"s1e","seq_no":7,"type":"shard_finished","shard":"shard-a"}
{"event_id":"rf1","seq_no":8,"type":"run_finished","status":"passed"}
JSONL
    ;;

  cancelled)
cat <<'JSONL'
{"event_id":"s1","seq_no":1,"type":"shard_started","shard":"shard-a"}
{"event_id":"k1s","seq_no":2,"type":"attempt_started","shard":"shard-a","test_id":"test-keep","attempt_id":"k-1","attempt_no":1}
{"event_id":"k1f","seq_no":3,"type":"attempt_finished","shard":"shard-a","test_id":"test-keep","attempt_id":"k-1","attempt_no":1,"status":"passed"}
{"event_id":"z1s","seq_no":4,"type":"attempt_started","shard":"shard-a","test_id":"test-zap","attempt_id":"z-1","attempt_no":1}
{"event_id":"rf1","seq_no":5,"type":"run_finished","status":"cancelled","cancelled":true}
{"event_id":"s1e","seq_no":6,"type":"shard_finished","shard":"shard-a"}
JSONL
    ;;

  missing)
cat <<'JSONL'
{"event_id":"s1","seq_no":1,"type":"shard_started","shard":"shard-a"}
{"event_id":"m1s","seq_no":2,"type":"attempt_started","shard":"shard-a","test_id":"test-maybe","attempt_id":"m-1","attempt_no":1}
{"event_id":"s1e","seq_no":3,"type":"shard_finished","shard":"shard-a"}
{"event_id":"rf1","seq_no":4,"type":"run_finished","status":"incomplete"}
JSONL
    ;;

  malformed)
cat <<'JSONL'
{"event_id":"s1","seq_no":1,"type":"shard_started","shard":"shard-a"}
this is not json at all
{"event_id":"g1s","seq_no":2,"type":"attempt_started","shard":"shard-a","test_id":"test-good","attempt_id":"g-1","attempt_no":1}
{"event_id":"broken","type":"attempt_finished"
{"event_id":"g1f","seq_no":3,"type":"attempt_finished","shard":"shard-a","test_id":"test-good","attempt_id":"g-1","attempt_no":1,"status":"passed"}
{"event_id":"s1e","seq_no":4,"type":"shard_finished","shard":"shard-a"}
{"event_id":"rf1","seq_no":5,"type":"run_finished","status":"passed"}
JSONL
    ;;

  *)
    echo "unknown scenario: $scenario" >&2
    echo "expected one of: happy retry crash shuffled duplicates late-write cancelled missing malformed" >&2
    exit 2
    ;;
esac
