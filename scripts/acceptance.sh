#!/usr/bin/env bash
# End-to-end acceptance demonstration for ecstripe.
#
# Builds the project, encodes a sample blob, enumerates loss patterns via the
# JSON control entry point, demonstrates over-loss failure, length mismatch,
# and silent-corruption detection via the external SHA-256.
#
# Usage: scripts/acceptance.sh [release|debug]
set -u

HERE="$(cd "$(dirname "$0")/.." && pwd)"
PROFILE="${1:-release}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cd "$HERE"

if [ "$PROFILE" = release ]; then
  cargo build --release
  BIN="$HERE/target/release/ecstripe"
else
  cargo build
  BIN="$HERE/target/debug/ecstripe"
fi

echo "== workdir: $WORK"
head -c 5000 /dev/urandom > "$WORK/input.bin"

jq_req() { jq -n --arg in "$1" --arg out "$2" "$3"; }

echo
echo "== 1. encode (k=3 m=2 S=1024, hash tagged) =="
jq -n --arg in "$WORK/input.bin" --arg out "$WORK/data.enc" \
  '{input:$in,output:$out,data_shards:3,parity_shards:2,stripe_size:1024,tag_hash:true}' \
  > "$WORK/encode.json"
"$BIN" encode "$WORK/encode.json"

echo
echo "== 2. info =="
echo "{\"input\":\"$WORK/data.enc\"}" > "$WORK/info.json"
"$BIN" info "$WORK/info.json"

echo
echo "== 3. decode losing shards 1 and 4 on every stripe (= m losses), verify hash =="
jq -n --arg in "$WORK/data.enc" --arg out "$WORK/decoded.ok.bin" \
  '{input:$in,output:$out,loss:{shards:[1,4]},expect_hash:true}' \
  > "$WORK/decode.json"
"$BIN" decode "$WORK/decode.json"
cmp "$WORK/input.bin" "$WORK/decoded.ok.bin" && echo "OUTPUT MATCHES INPUT"

echo
echo "== 4. decode losing 3 shards (> m=2): must fail =="
jq -n --arg in "$WORK/data.enc" --arg out "$WORK/decoded.x.bin" \
  '{input:$in,output:$out,loss:{shards:[0,1,2]},expect_hash:true}' \
  > "$WORK/decode_x.json"
"$BIN" decode "$WORK/decode_x.json"; echo "exit code: $?"

echo
echo "== 4b. inconsistent shard length (truncate one payload byte): must fail =="
python3 - "$WORK/data.enc" "$WORK/data.short.enc" <<'PY'
import sys
src, dst = sys.argv[1], sys.argv[2]
b = bytearray(open(src, "rb").read())
hlen = int.from_bytes(b[4:8], "big")
p = 8 + hlen
assert b[p] == 0x01, "expected a chunk record first"
# header: tag(1) stripe(4) shard(1) len(4)
old_len = int.from_bytes(b[p + 6 : p + 10], "big")
new_len = old_len - 1
b[p + 6 : p + 10] = new_len.to_bytes(4, "big")
# remove the last payload byte of this chunk, keep every later record intact
del b[p + 10 + new_len : p + 10 + old_len]
open(dst, "wb").write(b)
print(f"rewrote first chunk length {old_len} -> {new_len}")
PY
jq -n --arg in "$WORK/data.short.enc" --arg out "$WORK/decoded.short.bin" \
  '{input:$in,output:$out}' > "$WORK/decode_short.json"
"$BIN" decode "$WORK/decode_short.json"; echo "exit code: $?"


# Flip a byte in parity shard 3, then lose data shard 0: the decoder takes
# the three smallest survivor ids (1,2,3), so the flipped parity actually
# participates in reconstruction.
jq -n --arg in "$WORK/data.enc" --arg out "$WORK/data.corrupt.enc" \
  '{input:$in,output:$out,flips:[{stripe:0,shard:3,offset:0,xor:1}]}' \
  > "$WORK/corrupt.json"
"$BIN" corrupt "$WORK/corrupt.json"
jq -n --arg in "$WORK/data.corrupt.enc" --arg out "$WORK/decoded.corrupt.bin" \
  '{input:$in,output:$out,loss:{shards:[0]}}' \
  > "$WORK/decode_c.json"
"$BIN" decode "$WORK/decode_c.json"
if cmp -s "$WORK/input.bin" "$WORK/decoded.corrupt.bin"; then
  echo "UNEXPECTED: corrupted decode matched"
else
  echo "DECODED DATA IS WRONG (silent corruption invisible without external check)"
fi

echo
echo "== 6. same reconstruction with external SHA-256 verification: must fail =="
# Same erasure pattern as step 5 so the flipped parity participates; the
# plaintext hash now exposes the silently corrupted reconstruction.
jq -n --arg in "$WORK/data.corrupt.enc" --arg out "$WORK/decoded.checked.bin" \
  '{input:$in,output:$out,loss:{shards:[0]},expect_hash:true}' \
  > "$WORK/decode_h.json"
"$BIN" decode "$WORK/decode_h.json"; echo "exit code: $?"

echo
echo "== 7. unit + integration test suite =="
cargo test -- --nocapture
