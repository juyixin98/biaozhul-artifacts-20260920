#!/usr/bin/env bash
# Acceptance checklist for the aac adaptive arithmetic codec.
#
# Builds the release binary, then verifies the requirements:
#   * empty string round-trip
#   * buffer containing every byte value round-trip
#   * long skewed data triggers MANY rescales and round-trips
#   * every truncated prefix of a container is rejected
#   * deterministic (repeated encoding is byte-identical)
#   * JSON control entry encode/decode + error envelopes
#   * decode output-length cap is enforced
#   * large file streams with bounded resident memory
#
# Prints one "PASS"/"FAIL" line per check and exits non-zero if any failed.
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BIN="$ROOT/target/release/aac"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# Allow overriding the cargo/rustc on constrained machines.
CARGO="${CARGO:-cargo}"

pass=0
fail=0
check() { # check <name> <condition 0=ok>
    if [ "$2" -eq 0 ]; then echo "PASS  $1"; pass=$((pass + 1));
    else echo "FAIL  $1"; fail=$((fail + 1)); fi
}

echo "== build =="
( cd "$ROOT" && "$CARGO" build --release ) >/dev/null 2>"$WORK/build.err"
check "release build" $?
if [ ! -x "$BIN" ]; then
    echo "binary missing; build log:"; cat "$WORK/build.err"
    exit 1
fi

echo "== fixtures =="
: > "$WORK/empty.bin"
python3 -c "import sys;sys.stdout.buffer.write(bytes(range(256))*4)" > "$WORK/allbytes.bin"
python3 -c "
import sys,random
random.seed(1)
d=bytes(0 if random.random()<0.995 else random.randrange(256) for _ in range(200000))
sys.stdout.buffer.write(d)" > "$WORK/skewed.bin"

roundtrip() { # $1 = name
    "$BIN" encode -i "$WORK/$1.bin" -o "$WORK/$1.ac01" 2>/dev/null
    "$BIN" decode -i "$WORK/$1.ac01" -o "$WORK/$1.out" 2>/dev/null
    cmp -s "$WORK/$1.bin" "$WORK/$1.out"
}

echo "== round-trips =="
roundtrip empty;     check "empty string round-trip" $?
roundtrip allbytes;  check "all 256 byte values round-trip" $?
roundtrip skewed;   check "long skewed data round-trip" $?

echo "== rescaling =="
"$BIN" encode -i "$WORK/skewed.bin" -o "$WORK/skewed.ac01" 2>"$WORK/enc.log"
RESCALES=$(sed -n 's/.* \([0-9][0-9]*\) rescales.*/\1/p' "$WORK/enc.log")
echo "    skewed rescales = $RESCALES"
[ "${RESCALES:-0}" -ge 10 ]
check "skewed data triggers multiple rescales (>=10)" $?

echo "== determinism =="
"$BIN" encode -i "$WORK/skewed.bin" -o "$WORK/a.ac01" 2>/dev/null
"$BIN" encode -i "$WORK/skewed.bin" -o "$WORK/b.ac01" 2>/dev/null
cmp -s "$WORK/a.ac01" "$WORK/b.ac01"
check "output is deterministic" $?

echo "== truncation =="
SIZE=$(wc -c < "$WORK/skewed.ac01")
trunc_fail=0
for cut in 0 1 4 5 29 100 1000 $((SIZE-1)); do
    head -c "$cut" "$WORK/skewed.ac01" > "$WORK/t.ac01"
    if "$BIN" decode -i "$WORK/t.ac01" -o "$WORK/t.out" 2>/dev/null; then
        echo "    prefix of length $cut was ACCEPTED (bug)"
        trunc_fail=1
    fi
done
check "all sampled truncated prefixes rejected" $trunc_fail

echo "== JSON control entry =="
echo '{"op":"encode","data":""}' | "$BIN" json > "$WORK/j1.json" 2>/dev/null
grep -q '"ok":true' "$WORK/j1.json"; check "json encode empty succeeds" $?

B64=$(printf 'hello' | base64)
echo "{\"op\":\"encode\",\"data\":\"$B64\"}" | "$BIN" json > "$WORK/j2.json" 2>/dev/null
CONTAINER=$(python3 -c "import json;print(json.load(open('$WORK/j2.json'))['result'])")
echo "{\"op\":\"decode\",\"data\":\"$CONTAINER\"}" | "$BIN" json > "$WORK/j3.json" 2>/dev/null
python3 -c "import json,base64
d=json.load(open('$WORK/j3.json'));assert d['ok'] and base64.b64decode(d['result'])==b'hello'" \
    2>/dev/null
check "json encode/decode round-trips 'hello'" $?

echo '{"op":"decode","data":"QUMwMQE="}' | "$BIN" json > "$WORK/j4.json" 2>/dev/null
grep -q '"ok":false' "$WORK/j4.json"; check "json decode error envelope on bad input" $?

echo "== output cap =="
"$BIN" decode -i "$WORK/allbytes.ac01" -o "$WORK/cap.out" --max-output-bytes 100 2>/dev/null
[ $? -ne 0 ]; check "decode output-length cap enforced" $?

echo "== bounded memory on a 50 MB stream =="
python3 -c "import sys;sys.stdout.buffer.write(b'A'*50_000_000)" > "$WORK/big.bin"
if [ -x /usr/bin/time ] && /usr/bin/time -v true 2>/dev/null; then
    /usr/bin/time -v "$BIN" encode -i "$WORK/big.bin" -o "$WORK/big.ac01" 2>"$WORK/time.txt"
    RSS=$(sed -n 's/Maximum resident set size.*: //p' "$WORK/time.txt")
    "$BIN" decode -i "$WORK/big.ac01" -o "$WORK/big.out" 2>/dev/null
    cmp -s "$WORK/big.bin" "$WORK/big.out"; rt=$?
    echo "    peak RSS encode = ${RSS} KiB for 50 MB input"
    [ "${RSS:-99999999}" -lt 10240 ] && [ $rt -eq 0 ]
    check "50 MB stream round-trips with peak RSS < 10 MiB" $?
else
    "$BIN" encode -i "$WORK/big.bin" -o "$WORK/big.ac01" 2>/dev/null
    "$BIN" decode -i "$WORK/big.ac01" -o "$WORK/big.out" 2>/dev/null
    cmp -s "$WORK/big.bin" "$WORK/big.out"
    check "50 MB stream round-trips (RSS measurement unavailable)" $?
fi

echo
echo "RESULT: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
