#!/usr/bin/env bash
# 验收脚本：验证可重复打包的确定性保证。
# 用法: ./scripts/acceptance.sh
set -euo pipefail

cd "$(dirname "$0")/.."
PORT="${PORT:-$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"; kill "$SERVER_PID" 2>/dev/null || true' EXIT

echo "== 构建 =="
cargo build --quiet

echo "== 构造两份内容相同、创建顺序/mtime 不同的目录 =="
A="$WORK/a"; B="$WORK/b"
mkdir -p "$A/src/nested" "$B/src/nested"

# A：正序创建
printf 'hello reproducible world\n' > "$A/README.md"
printf 'fn main() {}\n'            > "$A/src/main.rs"
printf '\x00\x01\x02\x03\xff'     > "$A/src/nested/data.bin"
chmod 755 "$A/src/main.rs"

# B：逆序创建 + 完全不同的 mtime
printf '\x00\x01\x02\x03\xff'     > "$B/src/nested/data.bin"
printf 'fn main() {}\n'            > "$B/src/main.rs"
printf 'hello reproducible world\n' > "$B/README.md"
chmod 755 "$B/src/main.rs"
touch -d '1999-01-01 00:00:00 UTC' "$B/README.md" "$B/src/main.rs" "$B/src/nested/data.bin"
touch -d '2030-12-31 23:59:59 UTC' "$A/README.md" "$A/src/main.rs" "$A/src/nested/data.bin"

pack() { # $1=root 输出 sha256
  curl -sf -X POST "http://127.0.0.1:$PORT/pack" \
    -H 'content-type: application/json' \
    -d "{\"root\": \"$1\"}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["archive"]["sha256"])'
}

echo "== 启动服务（时区 America/New_York）=="
TZ=America/New_York PORT=$PORT ./target/debug/repro-pack &
SERVER_PID=$!
for i in $(seq 1 50); do curl -sf "http://127.0.0.1:$PORT/health" >/dev/null && break; sleep 0.1; done

SHA_A_NY="$(pack "$A")"
SHA_B_NY="$(pack "$B")"
echo "  A (NY): $SHA_A_NY"
echo "  B (NY): $SHA_B_NY"
[ "$SHA_A_NY" = "$SHA_B_NY" ] && echo "  PASS: 枚举顺序与 mtime 不同 → 摘要一致" || { echo "  FAIL"; exit 1; }
kill $SERVER_PID; wait $SERVER_PID 2>/dev/null || true

echo "== 重启服务（时区 Asia/Shanghai）=="
TZ=Asia/Shanghai PORT=$PORT ./target/debug/repro-pack &
SERVER_PID=$!
for i in $(seq 1 50); do curl -sf "http://127.0.0.1:$PORT/health" >/dev/null && break; sleep 0.1; done

SHA_A_SH="$(pack "$A")"
echo "  A (SH): $SHA_A_SH"
[ "$SHA_A_NY" = "$SHA_A_SH" ] && echo "  PASS: 宿主时区不同 → 摘要一致" || { echo "  FAIL"; exit 1; }

echo "== 冲突规范路径必须拒绝 =="
CODE=$(curl -s -o "$WORK/err.json" -w '%{http_code}' -X POST "http://127.0.0.1:$PORT/pack" \
  -H 'content-type: application/json' \
  -d "{\"root\": \"$A\", \"paths\": [\"src//main.rs\", \"src/./main.rs\"]}")
echo "  HTTP $CODE: $(cat "$WORK/err.json")"
[ "$CODE" = "409" ] && echo "  PASS: 冲突路径返回 409" || { echo "  FAIL: 期望 409"; exit 1; }

echo "== 路径穿越必须拒绝 =="
CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:$PORT/pack" \
  -H 'content-type: application/json' \
  -d "{\"root\": \"$A\", \"paths\": [\"../../etc/passwd\"]}")
[ "$CODE" = "400" ] && echo "  PASS: 路径穿越返回 400" || { echo "  FAIL: 期望 400, 得到 $CODE"; exit 1; }

echo "== 下载 tar 并用系统 tar 校验 =="
curl -sf -X POST "http://127.0.0.1:$PORT/pack" \
  -H 'content-type: application/json' -H 'accept: application/x-tar' \
  -d "{\"root\": \"$A\"}" -o "$WORK/out.tar"
tar -tvf "$WORK/out.tar"
sha256sum "$WORK/out.tar"

echo
echo "全部验收项通过。"
