#!/usr/bin/env bash
# End-to-end demo of the block-level delta artifact service.
#
# Requirements: bash, curl, python3 (fixtures + JSON only), cmp, a built
# release binary. The BLAKE3 digest used for end-to-end verification is
# produced by the service itself through POST /v1/digests.
#
# Scenarios: head insertion, local deletion, fully changed content.
#
# Usage: scripts/demo.sh
set -euo pipefail

cd "$(dirname "$0")/.."
PORT="${PORT:-8080}"
BASE="http://127.0.0.1:${PORT}"
WORK="$(mktemp -d)"
BLOCK_SIZE=512

cleanup() { kill "${SERVER_PID:-0}" 2>/dev/null || true; rm -rf "$WORK"; }
trap cleanup EXIT

echo "== building release binary =="
cargo build --release

echo "== starting server on ${BASE} =="
ARTIFACT_BIND="127.0.0.1:${PORT}" ./target/release/artifact-delta >"$WORK/server.log" 2>&1 &
SERVER_PID=$!
for _ in $(seq 1 50); do
  curl -sf "${BASE}/healthz" >/dev/null 2>&1 && break
  sleep 0.1
done

# Extract a JSON string field without jq.
jstr() { python3 -c "import json,sys;print(json.load(open(sys.argv[1]))$2)" "$1"; }

run_scenario () {
  local name="$1" basis_id="$2" basis_file="$3" target_file="$4"
  echo
  echo "================ scenario: ${name} ================"
  local basis_bytes target_bytes
  basis_bytes=$(stat -c%s "$basis_file")
  target_bytes=$(stat -c%s "$target_file")
  echo "basis : ${basis_id}  ${basis_bytes} bytes"
  echo "target: ${basis_id}-new ${target_bytes} bytes"

  curl -sf -X PUT --data-binary "@${basis_file}" "${BASE}/v1/artifacts/${basis_id}" >/dev/null
  curl -sf -X PUT --data-binary "@${target_file}" "${BASE}/v1/artifacts/${basis_id}-new" >/dev/null

  # signature
  curl -sf -X POST \
    -F "basis_id=${basis_id}" \
    -F "block_size=${BLOCK_SIZE}" \
    "${BASE}/v1/signatures" > "$WORK/sig.json"
  python3 -c "import json,sys;json.dump(json.load(open(sys.argv[1]))['signature'],open(sys.argv[2],'w'))" \
    "$WORK/sig.json" "$WORK/sig_obj.json"

  # delta. JSON objects go up via @file (avoids the OS ARG_MAX limit for very
  # large literals; the field name makes the server parse it as text either way).
  curl -sf -D "$WORK/delta.headers" -X POST \
    -F "signature=@${WORK}/sig_obj.json" \
    -F "data=@${target_file}" \
    "${BASE}/v1/deltas" > "$WORK/delta.json"
  python3 -c "import json,sys;json.dump(json.load(open(sys.argv[1]))['delta'],open(sys.argv[2],'w'))" \
    "$WORK/delta.json" "$WORK/delta_obj.json"

  echo "-- transfer stats (JSON body) --"
  python3 -c "import json,sys;print(json.dumps(json.load(open(sys.argv[1]))['stats'],indent=2))" "$WORK/delta.json"

  # expected digest, served by /v1/digests
  curl -sf -X POST --data-binary "@${target_file}" "${BASE}/v1/digests" > "$WORK/digest.json"
  local expected
  expected=$(jstr "$WORK/digest.json" "['blake3_hex']")

  # apply server-side
  curl -sf -X POST \
    -F "basis_id=${basis_id}" \
    -F "delta=@${WORK}/delta_obj.json" \
    -F "expected_blake3_hex=${expected}" \
    "${BASE}/v1/patch" > "$WORK/patch.json"

  python3 - "$WORK/patch.json" "$WORK/reconstructed.bin" <<'PY'
import base64, json, sys
p = json.load(open(sys.argv[1]))
assert p["verified"] is True, p
open(sys.argv[2], "wb").write(base64.b64decode(p["output_b64"]))
PY
  cmp "$target_file" "$WORK/reconstructed.bin"
  echo "VERIFY: reconstructed output byte-identical to target (cmp ok), server verified=true"
}

# ---- deterministic 256 KiB pseudo-random fixture ---------------------------
python3 - "$WORK/basis.bin" <<'PY'
import random, sys
random.seed(42)
open(sys.argv[1], "wb").write(bytes(random.getrandbits(8) for _ in range(262144)))
PY

# scenario 1: +1040 bytes inserted at head
python3 - "$WORK/basis.bin" "$WORK/target_head.bin" <<'PY'
import sys
prefix = b"HEAD-INSERTED-" * 80  # 1040 bytes
open(sys.argv[2], "wb").write(prefix + open(sys.argv[1], "rb").read())
PY
run_scenario "head insertion (+1040 B at offset 0)" head \
  "$WORK/basis.bin" "$WORK/target_head.bin"

# scenario 2: delete 4096 bytes in the middle
python3 - "$WORK/basis.bin" "$WORK/target_del.bin" <<'PY'
import sys
data = bytearray(open(sys.argv[1], "rb").read())
del data[100000:104096]
open(sys.argv[2], "wb").write(bytes(data))
PY
run_scenario "local deletion (-4096 B at offset 100000)" deletion \
  "$WORK/basis.bin" "$WORK/target_del.bin"

# scenario 3: entirely new content
python3 - "$WORK/target_changed.bin" <<'PY'
import random, sys
random.seed(7)
open(sys.argv[1], "wb").write(bytes(random.getrandbits(8) for _ in range(262144)))
PY
run_scenario "fully changed content (no shared blocks)" changed \
  "$WORK/basis.bin" "$WORK/target_changed.bin"

echo
echo "all scenarios completed successfully."
