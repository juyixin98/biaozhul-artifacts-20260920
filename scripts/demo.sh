#!/usr/bin/env bash
# End-to-end demo against a real running server (127.0.0.1:8080):
#   scenario A: head insertion   (2 KiB prepended to a 1 MiB artifact)
#   scenario B: local deletion   (100 KiB removed from the middle)
#   scenario C: constructed weak-checksum collision (must be strong-rejected)
# Prints transferred bytes / savings and verifies byte-identical rebuilds.
set -euo pipefail

PORT="${PORT:-8080}"
BASE="http://127.0.0.1:${PORT}"
BIN="$(dirname "$0")/../target/debug"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "== workdir: $WORK =="

python3 - "$WORK" <<'PY'
import os, sys, pathlib
w = pathlib.Path(sys.argv[1])
# Deterministic 1 MiB basis (incompressible-ish).
data = os.urandom(1024 * 1024)
(w / "v1.bin").write_bytes(data)
# A: 2 KiB inserted at the head.
(w / "v2-head.bin").write_bytes(os.urandom(2048) + data)
# B: delete bytes [400*1024, 500*1024) — 100 KiB, exactly block-aligned at 1024.
(w / "v2-del.bin").write_bytes(data[:409_600] + data[512_000:])
# C: weak-collision blocks, S=5.
a = bytes([10, 20, 30, 40, 50]); b = bytes([11, 17, 33, 39, 50])
tail = os.urandom(5)
(w / "c-old.bin").write_bytes(a + tail)
(w / "c-new.bin").write_bytes(b + tail)
print("fixtures ready:", [p.name for p in sorted(w.iterdir())])
PY

echo
echo "== scenario A: head insertion =="
curl -sS -X PUT --data-binary @"$WORK/v1.bin"      "$BASE/artifacts/demo"      -o /dev/null
curl -sS -X PUT --data-binary @"$WORK/v2-head.bin" "$BASE/artifacts/demo-head" -o /dev/null
cp "$WORK/v1.bin" "$WORK/local-head.bin"
"$BIN/delta-client" sync "$BASE" demo-head "$WORK/local-head.bin" 1024
cmp "$WORK/local-head.bin" "$WORK/v2-head.bin" && echo "VERIFY: byte-identical OK"

echo
echo "== scenario B: local deletion =="
curl -sS -X PUT --data-binary @"$WORK/v2-del.bin" "$BASE/artifacts/demo-del" -o /dev/null
cp "$WORK/v1.bin" "$WORK/local-del.bin"
"$BIN/delta-client" sync "$BASE" demo-del "$WORK/local-del.bin" 1024
cmp "$WORK/local-del.bin" "$WORK/v2-del.bin" && echo "VERIFY: byte-identical OK"

echo
echo "== scenario C: weak-checksum collision (block size 5) =="
curl -sS -X PUT --data-binary @"$WORK/c-old.bin" "$BASE/artifacts/c-old" -o /dev/null
curl -sS -X PUT --data-binary @"$WORK/c-new.bin" "$BASE/artifacts/c-new" -o /dev/null
cp "$WORK/c-old.bin" "$WORK/local-col.bin"
"$BIN/delta-client" sync "$BASE" c-new "$WORK/local-col.bin" 5
cmp "$WORK/local-col.bin" "$WORK/c-new.bin" && echo "VERIFY: byte-identical OK"

echo
echo "== re-sync an already-up-to-date file =="
"$BIN/delta-client" sync "$BASE" demo-head "$WORK/local-head.bin" 1024
