#!/usr/bin/env bash
# End-to-end smoke demo against a locally running tf_server.
# Signs requests with HMAC-SHA256. Requires: curl, python3.
set -euo pipefail

HOST="${TF_HOST:-127.0.0.1}"
PORT="${TF_PORT:-8080}"
KEY="${TF_HMAC_KEY:-demo-key}"
BASE="http://$HOST:$PORT"

sign() {  # args: METHOD TARGET BODY ; prints "ts sig"
  TF_METHOD="$1" TF_TARGET="$2" TF_BODY="$3" TF_KEY="$KEY" python3 - <<'PY'
import hashlib, hmac, os, time
method = os.environ["TF_METHOD"].encode()
target = os.environ["TF_TARGET"].encode()
body   = os.environ["TF_BODY"].encode()
key    = os.environ["TF_KEY"].encode()
ts = str(int(time.time()))
msg = method + b"\n" + target + b"\n" + ts.encode() + b"\n" + body
print(ts, hmac.new(key, msg, hashlib.sha256).hexdigest())
PY
}

post() {  # $1=path $2=json-body
  local path="$1" body="$2" tok ts sig
  tok="$(sign POST "$path" "$body")"; ts="${tok%% *}"; sig="${tok#* }"
  curl -sS -X POST "$BASE$path" \
    -H "Content-Type: application/json" \
    -H "X-TF-Timestamp: $ts" -H "X-TF-Signature: $sig" \
    -d "$body"
  echo
}

get() {  # $1=raw-target (may include query)
  local target="$1" tok ts sig
  tok="$(sign GET "$target" "")"; ts="${tok%% *}"; sig="${tok#* }"
  curl -sS "$BASE$target" \
    -H "X-TF-Timestamp: $ts" -H "X-TF-Signature: $sig"
  echo
}

echo "== health =="
curl -sS "$BASE/healthz"; echo

echo "== add static base->sensor (1m up) =="
post /v1/static '{"parent":"base","child":"sensor","transform":{"translation":[0,0,1],"rotation":{"w":1,"x":0,"y":0,"z":0}}}'

echo "== add two dynamic samples base->arm =="
post /v1/samples '{"parent":"base","child":"arm","samples":[
  {"stamp_us":0,"transform":{"translation":[0,0,0],"rotation":{"w":1,"x":0,"y":0,"z":0}}},
  {"stamp_us":1000000,"transform":{"translation":[1,0,0],"rotation":{"w":0.7071067811865476,"x":0,"y":0,"z":0.7071067811865476}}}
]}'

echo "== query sensor->arm at t=500000us (inverse + interpolated chain) =="
get "/v1/query?from=sensor&to=arm&time_us=500000"

echo "== same query in latest mode =="
get "/v1/query?from=sensor&to=arm&latest=1"

echo "== tree =="
get /v1/tree
