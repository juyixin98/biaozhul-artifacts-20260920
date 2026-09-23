#!/usr/bin/env bash
# 验收脚本：启动服务 -> 依次执行所有样例 -> 校验关键不变量 -> 关闭服务。
# 用法: ./scripts/accept.sh [--build-release]
set -euo pipefail

cd "$(dirname "$0")/.."

PORT="${PORT:-18099}"
BASE="http://127.0.0.1:${PORT}"
if [[ "${1:-}" == "--build-release" ]]; then
  PROFILE_FLAG="--release"
  BIN="target/release/gas-meter"
else
  PROFILE_FLAG=""
  BIN="target/debug/gas-meter"
fi

echo ">> building..."
cargo build $PROFILE_FLAG

LOG="$(mktemp)"
echo ">> starting server on $BASE (log: $LOG)"
"$BIN" --listen "127.0.0.1:${PORT}" --seed-file examples/seed.json >"$LOG" 2>&1 &
PID=$!
trap 'kill $PID 2>/dev/null || true' EXIT

for _ in $(seq 1 50); do
  if curl -sf "$BASE/healthz" >/dev/null 2>&1; then break; fi
  sleep 0.1
done

post() { curl -s -X POST "$BASE/v1/execute" -H 'content-type: application/json' -d @"$1"; }
status_of() { python3 -c 'import json,sys; print(json.load(sys.stdin)["receipt"]["status"])'; }
assert_eq() {
  if [[ "$1" != "$2" ]]; then echo "FAIL: $3 (got '$1', want '$2')"; exit 1; fi
  echo "ok: $3"
}

echo ">> 1. finite loop (real arithmetic + commit)"
R=$(post examples/requests/finite_loop.json)
assert_eq "$(echo "$R" | status_of)" "success" "finite loop succeeds"
JSON="$R" python3 - <<'PY'
import json,os,base64
r=json.loads(os.environ["JSON"])["receipt"]
out=base64.b64decode(r["output_base64"])
assert int.from_bytes(out[:4],"little")==55, out.hex()
assert int.from_bytes(out[4:],"little")==385, out.hex()
assert r["committed"] is True
print("ok: arithmetic 55/385 verified, writes committed")
PY

echo ">> 2. determinism: repeat same request, result_hash identical"
H1=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin)["receipt"]["result_hash"])')
H2=$(post examples/requests/finite_loop.json | python3 -c 'import json,sys; print(json.load(sys.stdin)["receipt"]["result_hash"])')
assert_eq "$H1" "$H2" "identical input+version => identical result_hash"

echo ">> 3. infinite loop terminated by fuel"
assert_eq "$(post examples/requests/infinite_loop.json | status_of)" "out_of_fuel" "infinite loop -> out_of_fuel"

echo ">> 4. trap-after-write rolls back"
assert_eq "$(post examples/requests/trap_after_write.json | status_of)" "trap" "trap_after_write -> trap"
if curl -s "$BASE/v1/state" | grep -q poisoned; then
  echo "FAIL: poisoned key committed despite trap"; exit 1
fi
echo "ok: poisoned key absent after trap"

echo ">> 5. memory grow within/over limit"
assert_eq "$(post examples/requests/memory_grow_ok.json | status_of)" "success" "memory grow within limit"
assert_eq "$(post examples/requests/memory_grow_denied.json | status_of)" "memory_limit_exceeded" "memory grow over limit"

echo ">> 6. host pointer OOB"
assert_eq "$(post examples/requests/host_oob.json | status_of)" "trap" "host oob -> trap"

echo ">> 7. protocol: length-prefixed framing + real CRC-32"
JSON="$(post examples/requests/frame_protocol.json)" python3 - <<'PY'
import json,os,struct,zlib,base64
r=json.loads(os.environ["JSON"])["receipt"]
assert r["status"]=="success", r["status"]
out=base64.b64decode(r["output_base64"])
pos=idx=0
expect=[b"ping", b"pong-1234567890", bytes([0,1,2,3,255])]
while pos<len(out):
    ln=struct.unpack_from("<H",out,pos)[0]; pos+=2
    payload=out[pos:pos+ln]; pos+=ln
    crc=struct.unpack_from("<I",out,pos)[0]; pos+=4
    assert payload==expect[idx] and crc==(zlib.crc32(payload)&0xffffffff)
    idx+=1
assert idx==3 and r["committed_writes"]==[["proto","03000000"]]
print("ok: 3 frames re-encoded, CRC-32 matches zlib, proto=3 committed")
PY
assert_eq "$(post examples/requests/frame_protocol_truncated.json | status_of)" "module_abort" "truncated frame rejected"

echo ">> 8. real SHA-256 cross-checked against python hashlib"
JSON="$(post examples/requests/sha256guest_abc.json)" python3 - <<'PY'
import json,os,base64,hashlib
r=json.loads(os.environ["JSON"])["receipt"]
assert r["status"]=="success", r["status"]
got=base64.b64decode(r["output_base64"]).hex()
want=hashlib.sha256(b"abc").hexdigest()
assert got==want, (got,want)
print(f"ok: sha256(abc)={got}")
PY

echo ">> 9. iterated SHA-256 (10 rounds) under v2 pricing"
JSON="$(post examples/requests/sha256guest_iter10.json)" python3 - <<'PY'
import json,os,base64,hashlib
r=json.loads(os.environ["JSON"])["receipt"]
assert r["status"]=="success", r["status"]
d=hashlib.sha256(b"chain").digest()
for _ in range(9): d=hashlib.sha256(d).digest()
assert base64.b64decode(r["output_base64"])==d
assert r["metering_version"]==2
print(f"ok: 10-round chain matches, v2 host_fuel={r['host_fuel_consumed']}")
PY

echo ">> 10. cryptographic work killed by low fuel, nothing committed"
JSON="$(post examples/requests/sha256guest_low_fuel.json)" python3 - <<'PY'
import json,os
r=json.loads(os.environ["JSON"])["receipt"]
assert r["status"]=="out_of_fuel", r["status"]
assert r["committed"] is False
print("ok: low-fuel crypto run killed, no commit")
PY

echo ">> 11. whitelist: wasi-importing / unknown-host modules are rejected"
# 由自动化测试覆盖（HTTP 400 与实例化前拒绝两条路径）
cargo test --test http_api whitelist_violation_is_reported_as_400 >/dev/null
cargo test --test sandbox_security rejects_wasi_imports >/dev/null
cargo test --test sandbox_security rejects_unknown_gas_fn >/dev/null
echo "ok: whitelist rejection covered by automated tests"

echo
echo "ALL ACCEPTANCE CHECKS PASSED"
