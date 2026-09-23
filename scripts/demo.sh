#!/usr/bin/env bash
# Reproducible end-to-end demo for merkle-store.
# Builds a repo, serves it over HTTP, fetches range proofs, and verifies
# them both online (independent verifier process) and offline (CLI).
#
# Usage:  bash scripts/demo.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN="$ROOT/target/release/merkle-store"
WORK="$(mktemp -d -t merkle-demo-XXXXXX)"
ADDR="127.0.0.1:18280"
VADDR="127.0.0.1:18281"

if [[ ! -x "$BIN" ]]; then
  echo "building release binary…"
  cargo build --release --manifest-path "$ROOT/Cargo.toml" >/dev/null
fi

say() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
cleanup() {
  [[ -n "${SRV_PID:-}" ]] && kill "$SRV_PID" 2>/dev/null || true
  [[ -n "${VRF_PID:-}" ]] && kill "$VRF_PID" 2>/dev/null || true
}
trap cleanup EXIT

say "work dir: $WORK"
REPO="$WORK/repo"; VRF="$WORK/verifier"
"$BIN" init "$REPO" --block-size 4

say "start server on $ADDR"
"$BIN" serve "$REPO" --addr "$ADDR" &
SRV_PID=$!
sleep 0.3

say "build an 11-byte file (3 blocks of 4/4/3)"
printf 'abcdefghijk' > "$WORK/payload.bin"
curl -s -X POST --data-binary "@$WORK/payload.bin" "http://$ADDR/build"
echo

ROOT_B64=$(curl -s "$ADDR/root" | python3 -c 'import sys,json;print(json.load(sys.stdin)["root"])')
say "trusted root: $ROOT_B64"

say "first-block range proof"
curl -s "$ADDR/range?which=first" | tee "$WORK/first.json"
echo

say "last-block (short) range proof"
curl -s "$ADDR/range?which=last" | tee "$WORK/last.json"
echo

# A second, completely independent verifier rooted at an EMPTY repo.
"$BIN" init "$VRF" --block-size 4
"$BIN" serve "$VRF" --addr "$VADDR" &
VRF_PID=$!
sleep 0.3

say "verify first proof on the INDEPENDENT verifier ($VADDR, empty repo)"
curl -s -X POST --data-binary "@$WORK/first.json" "http://$VADDR/verify"; echo
say "verify last proof on the INDEPENDENT verifier"
curl -s -X POST --data-binary "@$WORK/last.json" "http://$VADDR/verify"; echo

say "offline CLI verify (no server)"
"$BIN" verify "$WORK/first.json"; echo

# Negative checks are EXPECTED to fail validation and exit non-zero; keep
# set -e from aborting the script there.
say "negative tests: tampered block byte"
python3 - "$WORK/first.json" "$WORK/bad-block.json" <<'PY'
import sys,json,base64
d=json.load(open(sys.argv[1]))
b=bytearray(base64.b64decode(d["blocks"][0]["data"])); b[0]^=1
d["blocks"][0]["data"]=base64.b64encode(b).decode()
json.dump(d,open(sys.argv[2],"w"))
PY
"$BIN" verify "$WORK/bad-block.json" || true
say "negative tests: forged data_len"
sed 's/"data_len": 11/"data_len": 99/' "$WORK/first.json" > "$WORK/bad-len.json"
"$BIN" verify "$WORK/bad-len.json" || true
say "negative tests: swapped (misaligned) proof nodes"
python3 - "$WORK/first.json" "$WORK/bad-proof.json" <<'PY'
import sys,json
d=json.load(open(sys.argv[1]))
d["proof"][1],d["proof"][2]=d["proof"][2],d["proof"][1]
json.dump(d,open(sys.argv[2],"w"))
PY
"$BIN" verify "$WORK/bad-proof.json" || true

say "empty file: reset to empty and verify"
curl -s -X POST --data '' "$ADDR/build" >/dev/null
curl -s "$ADDR/range?which=first" > "$WORK/empty.json"
cat "$WORK/empty.json"; echo
curl -s -X POST --data-binary "@$WORK/empty.json" "http://$VADDR/verify"; echo

say "demo complete; working copy kept at: $WORK"
trap - EXIT
cleanup
echo "repo left in $WORK"
