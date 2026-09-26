#!/usr/bin/env bash
# End-to-end acceptance demo for BSE1 schema evolution.
# Runs the full encode -> old-schema forward -> new-schema decode chain,
# plus incompatibility, presence and truncation checks.
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
B="${BSCHEMA_BIN:-target/release/bschema}"
S1=examples/schemas/person_v1.json
S2=examples/schemas/person_v2.json
SB=examples/schemas/person_incompatible.json
mkdir -p build

pass=0
fail=0
check() { # desc expected_exit actual_exit
  if [ "$2" = "$3" ]; then echo "PASS: $1"; pass=$((pass+1));
  else echo "FAIL: $1 (expected exit $2, got $3)"; fail=$((fail+1)); fi
}

echo "== build =="
cargo build --release --quiet
[ -x "$B" ] || { echo "binary missing: $B"; exit 2; }

echo "== 1. fingerprints (rename-safe, version-distinct) =="
fp1=$($B fingerprint --schema "$S1")
fp2=$($B fingerprint --schema "$S2")
echo "v1=$fp1 v2=$fp2"
[ "$fp1" != "$fp2" ]; check "v1 != v2 fingerprint" 0 $?

echo "== 2. encode v2 message =="
$B encode --schema "$S2" --message examples/messages/person_v2_full.json --out build/v2.bse1

echo "== 3. v2 decodes v2 =="
$B decode --schema "$S2" --input build/v2.bse1 --meta > build/dec_v2.json
grep -q '"fingerprint_match": true' build/dec_v2.json; check "same-schema fingerprint match" 0 $?

echo "== 4. OLD v1 reads NEW v2 data (unknown retained) =="
$B decode --schema "$S1" --input build/v2.bse1 > build/dec_v1.json
grep -q '"number": 7' build/dec_v1.json; check "unknown nickname retained" 0 $?

echo "== 5. v1 proxy forwards =="
$B forward --schema "$S1" --input build/v2.bse1 --out build/via_v1.bse1
tail -c +15 build/v2.bse1     > build/body_orig.bin
tail -c +15 build/via_v1.bse1 > build/body_fwd.bin
cmp -s build/body_orig.bin build/body_fwd.bin; check "forwarded body byte-identical" 0 $?

echo "== 6. v2 fully recovers after the proxy =="
$B decode --schema "$S2" --input build/via_v1.bse1 > build/final.json
grep -q '"nickname": "Ada"' build/final.json; check "nickname recovered" 0 $?
grep -q '"country": "UK"'   build/final.json; check "nested unknown recovered" 0 $?

echo "== 7. OLD v1 data read by NEW v2 schema =="
$B encode --schema "$S1" --message examples/messages/person_full.json --out build/v1.bse1
$B decode --schema "$S2" --input build/v1.bse1 > build/v1_by_v2.json
grep -q '"id": 42' build/v1_by_v2.json; check "old data still decodes" 0 $?

echo "== 8. incompatible type change rejected =="
$B decode --schema "$SB" --input build/v2.bse1 >/dev/null 2>&1
check "string->int64 rejected" 1 $?

echo "== 9. explicit zero vs missing =="
$B encode --schema "$S1" --message examples/messages/person_zeros.json --out build/zero.bse1
$B decode --schema "$S1" --input build/zero.bse1 > build/zero.json
grep -q '"id": 0'  build/zero.json; check "explicit id=0 present" 0 $?
grep -q '"email"'  build/zero.json && check "missing email omitted" 1 0 || check "missing email omitted" 1 1

echo "== 10. truncation rejected =="
sz=$(stat -c %s build/v2.bse1)
head -c $((sz-5)) build/v2.bse1 > build/trunc.bse1
$B decode --schema "$S2" --input build/trunc.bse1 >/dev/null 2>&1
check "truncated input rejected" 1 $?

echo "== 11. resource limit =="
$B decode --schema "$S2" --input build/v2.bse1 --max-bytes 16 >/dev/null 2>&1
check "byte limit enforced" 1 $?

echo
echo "RESULT: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
