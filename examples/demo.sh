#!/usr/bin/env bash
# 端到端演示：构建端构建/出补丁 -> 设备端下发补丁/原子应用 -> 核验最终摘要。
# 纯本地，不连接任何云平台。构建命令是显式提供的测试夹具（cp 夹具文件）。
#
# 用法: bash examples/demo.sh [监听端口基号]
set -euo pipefail

BASE=${1:-19000}
BPORT=$BASE
DPORT=$((BASE+1))
ROOT=$(mktemp -d /tmp/delta-demo.XXXXXX)
BIN=$ROOT/deltaupd
trap 'kill $BUILDER_PID $DEVICE_PID 2>/dev/null || true' EXIT

echo "==> 编译: go build -o $BIN ./cmd/deltaupd"
go build -o "$BIN" ./cmd/deltaupd

mkdir -p "$ROOT/builder" "$ROOT/device" "$ROOT/fixtures"
python3 - "$ROOT" <<'PY'
import sys
root = sys.argv[1]
v1 = bytes((i*7+3) % 256 for i in range(200*1024))
insert = b"## INSERTED BLOCK FOR V2 ##" * 40
v2 = v1[:100*1024] + insert + v1[100*1024:] + b"TAIL-MOD"
open(f"{root}/fixtures/v1.bin", "wb").write(v1)
open(f"{root}/fixtures/v2.bin", "wb").write(v2)
print(f"夹具: v1={len(v1)} 字节, v2={len(v2)} 字节")
PY

"$BIN" -addr 127.0.0.1:$BPORT \
  -cache "$ROOT/builder/cache" -state "$ROOT/builder/state" -work "$ROOT/builder/work" \
  >"$ROOT/builder.log" 2>&1 &
BUILDER_PID=$!
sleep 0.5
echo "==> 构建端已启动: http://127.0.0.1:$BPORT"

req() { curl -sS --max-time 10 "$@"; }

echo
echo "== 1. 注册项目（构建命令为显式夹具: cp v2.bin artifact.bin）"
req -X POST 127.0.0.1:$BPORT/v1/projects -d \
  "{\"name\":\"app\",\"argv\":[\"cp\",\"$ROOT/fixtures/v2.bin\",\"artifact.bin\"],\"artifact_path\":\"artifact.bin\"}"
echo

echo "== 2. 上传 v1 基线制品"
OLD=$(req -X POST --data-binary @"$ROOT/fixtures/v1.bin" 127.0.0.1:$BPORT/v1/artifacts/app \
  | python3 -c 'import json,sys;print(json.load(sys.stdin)["data"]["digest"])')
echo "v1 digest: $OLD"

echo "== 3. 构建 v2（运行夹具命令）"
NEW=$(req -X POST 127.0.0.1:$BPORT/v1/projects/app/build -d '{}' \
  | python3 -c 'import json,sys;print(json.load(sys.stdin)["data"]["digest"])')
echo "v2 digest: $NEW"

echo "== 4. 生成 v1->v2 差量补丁"
req -X POST 127.0.0.1:$BPORT/v1/patches \
  -d "{\"name\":\"app\",\"old_digest\":\"$OLD\",\"new_digest\":\"$NEW\"}" | tee "$ROOT/patch.json"
echo
PD=$(python3 -c 'import json;print(json.load(open("'$ROOT'/patch.json"))["data"]["digest"])')
req 127.0.0.1:$BPORT/v1/patches/$PD/download -o "$ROOT/patch.bin"
echo "补丁大小: $(stat -c%s "$ROOT/patch.bin") 字节（全量 v2: $(stat -c%s "$ROOT/fixtures/v2.bin") 字节）"

# ---- 设备端（独立目录，模拟另一台机器）----
"$BIN" -addr 127.0.0.1:$DPORT \
  -cache "$ROOT/device/cache" -state "$ROOT/device/state" -work "$ROOT/device/work" \
  >"$ROOT/device.log" 2>&1 &
DEVICE_PID=$!
sleep 0.5
echo
echo "==> 设备端已启动: http://127.0.0.1:$DPORT"

echo "== 5. 设备首装 v1 + 下发差量补丁"
req -X POST --data-binary @"$ROOT/fixtures/v1.bin" 127.0.0.1:$DPORT/v1/artifacts/app >/dev/null
req -X POST --data-binary @"$ROOT/patch.bin" 127.0.0.1:$DPORT/v1/patches/ingest
echo

echo "== 6. 错基线演示：设备先更新到 v2，再应用同一 v1->v2 补丁（基线不符，应 422）"
req -X POST 127.0.0.1:$DPORT/v1/apply -d "{\"name\":\"app\",\"patch_digest\":\"$PD\"}" >/dev/null
req -w "\nHTTP %{http_code}\n" -X POST 127.0.0.1:$DPORT/v1/apply \
  -d "{\"name\":\"app\",\"patch_digest\":\"$PD\"}" || true
# 把设备当前制品重置回 v1，便于后续损坏补丁测试
req -X POST --data-binary @"$ROOT/fixtures/v1.bin" 127.0.0.1:$DPORT/v1/artifacts/app >/dev/null

echo "== 7. 损坏补丁演示：翻转补丁体一个字节"
python3 - "$ROOT/patch.bin" "$ROOT/patch-bad.bin" <<'PY'
import sys
d = bytearray(open(sys.argv[1],"rb").read())
d[-700] ^= 0xFF
open(sys.argv[2],"wb").write(d)
PY
BAD=$(req -X POST --data-binary @"$ROOT/patch-bad.bin" 127.0.0.1:$DPORT/v1/patches/ingest \
  | python3 -c 'import json,sys;print(json.load(sys.stdin)["data"]["digest"])')
req -w "\nHTTP %{http_code}\n" -X POST 127.0.0.1:$DPORT/v1/apply \
  -d "{\"name\":\"app\",\"patch_digest\":\"$BAD\"}" || true
echo -n "失败后设备当前制品（必须仍是 v1）: "
req 127.0.0.1:$DPORT/v1/artifacts/app/current | python3 -c 'import json,sys;d=json.load(sys.stdin)["data"];print(d["digest"])'

echo "== 8. 应用正确补丁（原子切换）"
req -X POST 127.0.0.1:$DPORT/v1/apply -d "{\"name\":\"app\",\"patch_digest\":\"$PD\"}"
echo

echo "== 9. 核验最终摘要与内容"
req 127.0.0.1:$DPORT/v1/artifacts/app/download -o "$ROOT/dev-current.bin"
echo -n "设备制品 sha256: "; sha256sum "$ROOT/dev-current.bin" | cut -d' ' -f1
echo -n "期望 v2 sha256:  "; sha256sum "$ROOT/fixtures/v2.bin" | cut -d' ' -f1
cmp "$ROOT/dev-current.bin" "$ROOT/fixtures/v2.bin" && echo "✓ 内容逐字节一致"

echo
echo "==> 演示完成，临时目录: $ROOT"
trap - EXIT
kill $BUILDER_PID $DEVICE_PID 2>/dev/null || true
