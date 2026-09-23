#!/usr/bin/env bash
# Runnable example session for the streaming quantile summary service.
#
# Prereqs: service running (scripts/run.sh) and `curl` + `jq` available.
#   BASE=http://localhost:8080 examples/requests.sh
set -euo pipefail
BASE="${BASE:-http://localhost:8080}"

say() { printf '\n=== %s ===\n' "$*"; }

say "health"
curl -s "$BASE/healthz"; echo

say "create three shards with the SAME epsilon (required for merge)"
for sh in shard-eu shard-us shard-ap; do
  curl -s -X PUT "$BASE/v1/summaries/$sh" \
    -H 'Content-Type: application/json' -d '{"epsilon":0.01}' >/dev/null
done

say "stream 1..300000 into shard-eu"
seq 1 300000 | curl -s -X POST \
  "$BASE/v1/summaries/shard-eu/observations/stream" --data-binary @- \
  | jq -c '{id,count,storedTuples,errorBoundRank}'

say "batch JSON observations into shard-us (heavy tail) and shard-ap (ties)"
python3 - <<'PY' | curl -s -X POST "$BASE/v1/summaries/shard-us/observations" \
  -H 'Content-Type: application/json' --data-binary @- \
  | jq -c '{id,count,storedTuples,errorBoundRank}'
import json, random
random.seed(20260923)
vals = [1.0 / (1.0 - random.random()) ** (1.0/1.1) for _ in range(200000)]
print(json.dumps({"values": vals}))
PY
python3 - <<'PY' | curl -s -X POST "$BASE/v1/summaries/shard-ap/observations" \
  -H 'Content-Type: application/json' --data-binary @- \
  | jq -c '{id,count,storedTuples,errorBoundRank}'
import json, random
random.seed(42)
print(json.dumps({"values": [random.randint(0, 9) for _ in range(150000)]}))
PY

say "quantiles of the EU shard"
curl -s "$BASE/v1/summaries/shard-eu/quantile?qs=0.5,0.9,0.99,0.999" | jq '.results'

say "rank / CDF of a value on the US shard"
curl -s "$BASE/v1/summaries/shard-us/rank?value=3.0" | jq -c \
  '{value,estimatedRank,cdf,errorBoundRank,count}'

say "export the EU snapshot and re-import it under a new id"
curl -s "$BASE/v1/summaries/shard-eu/snapshot" \
  | jq -c '{snapshot:.snapshot}' \
  | curl -s -X PUT "$BASE/v1/summaries/shard-eu-copy/snapshot" \
      -H 'Content-Type: application/json' --data-binary @- \
  | jq -c '{id,count,storedTuples}'

say "merge the three shards into a NEW summary"
curl -s -X POST "$BASE/v1/merge" -H 'Content-Type: application/json' \
  -d '{"id":"orders-all","sources":["shard-eu","shard-us","shard-ap"]}' \
  | jq -c '{id,count,storedTuples,errorBoundRank}'

say "quantiles of the merged union"
curl -s "$BASE/v1/summaries/orders-all/quantile?qs=0.5,0.9,0.99,0.999,1" \
  | jq '.results'

say "min/max are preserved exactly through merge"
curl -s "$BASE/v1/summaries/orders-all/quantile?q=0" | jq -c '.results[0]'
curl -s "$BASE/v1/summaries/orders-all/quantile?q=1" | jq -c '.results[0]'

say "merge with a DIFFERENT epsilon is rejected (HTTP 422)"
curl -s -X PUT "$BASE/v1/summaries/wrong-eps" \
  -H 'Content-Type: application/json' -d '{"epsilon":0.02}' >/dev/null
curl -s -X POST "$BASE/v1/summaries/wrong-eps/observations" \
  -H 'Content-Type: application/json' -d '{"values":[1,2,3]}' >/dev/null
curl -s -o /tmp/reject.json -w 'HTTP %{http_code}\n' -X POST "$BASE/v1/merge" \
  -H 'Content-Type: application/json' \
  -d '{"id":"bad-merge","sources":["shard-eu","wrong-eps"]}'
jq -c . /tmp/reject.json

say "list all summaries"
curl -s "$BASE/v1/summaries" | jq -c '.summaries[] | {id,count,storedTuples}'

say "cleanup"
for sh in shard-eu shard-us shard-ap shard-eu-copy wrong-eps orders-all; do
  curl -s -X DELETE "$BASE/v1/summaries/$sh" >/dev/null
done
echo "deleted example summaries"
