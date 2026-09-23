#!/usr/bin/env bash
# 故障演练: 篡改磁盘上的块字节 → /verify 必须发现; 带 ?verify=1 的下载必须拒绝。
# 用法: ./examples/corrupt_and_verify.sh REPO_DIR [HOST:PORT]
set -euo pipefail

REPO="${1:?usage: corrupt_and_verify.sh REPO_DIR [HOST:PORT]}"
ADDR="${2:-127.0.0.1:8080}"
BASE="http://$ADDR"

say() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

say "1. 上传一个块并记录哈希"
H=$(curl -s -X POST --data-binary 'integrity-check-me' "$BASE/blocks" | python3 -c 'import sys,json;print(json.load(sys.stdin)["hash"])')
echo "hash = $H"

say "2. 直接篡改磁盘上的块文件 (模拟磁盘损坏/本地恶意修改)"
FILE="$REPO/blocks/${H:0:2}/$H"
python3 - "$FILE" <<'EOF'
import sys
p = sys.argv[1]
b = bytearray(open(p, 'rb').read())
b[0] ^= 0xFF
open(p, 'wb').write(bytes(b))
print(f"flipped first byte of {p}")
EOF

say "3. 普通 GET 仍返回字节 (存储层不主动重哈希)"
curl -s "$BASE/blocks/$H"; echo

say "4. ?verify=1 的 GET 必须拒绝 → 422 corrupt"
curl -s -w "\nHTTP %{http_code}\n" "$BASE/blocks/$H?verify=1"

say "5. /verify 扫描必须报告该块"
curl -s -X POST "$BASE/verify"; echo
