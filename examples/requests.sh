#!/usr/bin/env bash
# encrypted_range_store 请求样例（curl）。
# 用法：bash examples/requests.sh
# 脚本自行选择空闲本地端口，启动服务后执行全部样例。
set -euo pipefail

cd "$(dirname "$0")/.."

PORT=$(python3 - <<'EOF'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
EOF
)
DATA_DIR=$(mktemp -d)
trap 'kill $(jobs -p) 2>/dev/null || true; rm -rf "$DATA_DIR"' EXIT

# 64 字节小块，便于在很小的数据上演示"满块 + 最后短块 + 跨块"。
python3 -m encrypted_range_store --data-dir "$DATA_DIR" \
  --block-size 64 serve --port "$PORT" &
sleep 1
B="http://127.0.0.1:$PORT"

echo "## healthz"
curl -s "$B/healthz"

echo "## 准备 200 字节样本（3 个满块 + 8 字节短块）"
python3 -c "open('/tmp/ers-sample.bin','wb').write(bytes((i*7)%256 for i in range(200)))"
open /tmp/ers-sample.bin 2>/dev/null || true

echo "## PUT /objects/demo"
curl -s -X PUT --data-binary @/tmp/ers-sample.bin "$B/objects/demo"
echo

echo "## PUT 空对象"
: >/tmp/ers-empty.bin
curl -s -X PUT --data-binary @/tmp/ers-empty.bin "$B/objects/empty"
echo

echo "## GET 整对象（200，sha256 与源文件一致）"
curl -s "$B/objects/demo" -o /tmp/ers-back.bin
sha256sum /tmp/ers-sample.bin /tmp/ers-back.bin

echo "## 首范围 bytes=0-9（206）"
curl -s -D - -o /tmp/ers-seg.bin -H 'Range: bytes=0-9' "$B/objects/demo" \
  | grep -Ei 'HTTP/|Content-Range|Content-Length'
xxd /tmp/ers-seg.bin

echo "## 尾范围 bytes=195-199（206，最后短块内）"
curl -s -H 'Range: bytes=195-199' "$B/objects/demo" | xxd

echo "## 跨块范围 bytes=60-140（206，横跨第 1/2/3 块）"
curl -s -D - -H 'Range: bytes=60-140' "$B/objects/demo" -o /tmp/ers-seg.bin \
  | grep -Ei 'HTTP/|Content-Range'
python3 - <<'EOF'
src = open('/tmp/ers-sample.bin','rb').read()
seg = open('/tmp/ers-seg.bin','rb').read()
assert seg == src[60:141]
print("正文与明文切片一致，长度", len(seg))
EOF

echo "## 空对象：GET 200 空体；Range -> 416"
curl -s -o /dev/null -w 'GET empty -> %{http_code} size=%{size_download}\n' \
  "$B/objects/empty"
curl -s -o /dev/null -w 'Range empty -> %{http_code}\n' \
  -H 'Range: bytes=0-0' "$B/objects/empty"

echo "## 篡改密文 -> 409（响应无明文）"
export DATA_DIR
python3 - <<'EOF'
import os
p = os.path.join(os.environ["DATA_DIR"], "objects", "demo")
b = bytearray(open(p, "rb").read())
b[-30] ^= 0xFF
open(p, "wb").write(b)
print("已翻转数据块密文区一个字节")
EOF
curl -s -w '\nGET 篡改对象 -> %{http_code}\n' "$B/objects/demo"

echo "## 非法 / 越界 Range"
# 先恢复对象
curl -s -X PUT --data-binary @/tmp/ers-sample.bin "$B/objects/demo" -o /dev/null
curl -s -o /dev/null -w 'bytes=200-  -> %{http_code} (期望416)\n' \
  -H 'Range: bytes=200-' "$B/objects/demo"
curl -s -o /dev/null -w 'bytes=x-y  -> %{http_code} (期望400)\n' \
  -H 'Range: bytes=x-y' "$B/objects/demo"
curl -s -o /dev/null -w '多区间      -> %{http_code} (期望400)\n' \
  -H 'Range: bytes=0-1,2-3' "$B/objects/demo"

echo "## 密文交换 -> 409"
python3 -c "open('/tmp/ers-A.bin','wb').write(b'AAAA-secret-A'*20)"
python3 -c "open('/tmp/ers-B.bin','wb').write(b'BBBB-other-B-'*20)"
curl -s -X PUT --data-binary @/tmp/ers-A.bin "$B/objects/A" -o /dev/null
curl -s -X PUT --data-binary @/tmp/ers-B.bin "$B/objects/B" -o /dev/null
cp "$DATA_DIR/objects/B" "$DATA_DIR/objects/A"
curl -s -w '\n读取被替换的 A -> %{http_code} (期望409)\n' "$B/objects/A"
curl -s -o /dev/null -w '读取未受影响的 B -> %{http_code} (期望200)\n' "$B/objects/B"

echo
echo "全部样例执行完毕。"
