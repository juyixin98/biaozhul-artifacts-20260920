#!/usr/bin/env bash
# cas-store 请求样例：完整走一遍 上传 → 发布根 → 切换根 → GC → 校验。
# 用法: ./examples/requests.sh [HOST:PORT]   (默认 127.0.0.1:8080)
# 前置: 已启动  cargo run -- /tmp/cas-demo
set -euo pipefail

ADDR="${1:-127.0.0.1:8080}"
BASE="http://$ADDR"
say() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

say "0. 健康检查"
curl -s "$BASE/healthz"; echo

say "1. 上传两个叶子块 (POST /blocks, 服务端算哈希)"
L1=$(curl -s -X POST --data-binary 'leaf-one-contents' "$BASE/blocks" | tee /dev/stderr | python3 -c 'import sys,json;print(json.load(sys.stdin)["hash"])')
L2=$(curl -s -X POST --data-binary 'leaf-two-contents' "$BASE/blocks" | tee /dev/stderr | python3 -c 'import sys,json;print(json.load(sys.stdin)["hash"])')

say "2. 同内容去重 (再次上传 leaf-one)"
curl -s -X POST --data-binary 'leaf-one-contents' "$BASE/blocks"; echo

say "3. 上传父块, 通过 X-Refs 声明引用"
P=$(curl -s -X POST -H "X-Refs: $L1,$L2" --data-binary 'tree-v1' "$BASE/blocks" | tee /dev/stderr | python3 -c 'import sys,json;print(json.load(sys.stdin)["hash"])')

say "4. 内容寻址上传 (PUT /blocks/<hash>, 客户端先算哈希)"
DATA='addressed-by-client'
H=$(printf %s "$DATA" | sha256sum | cut -d' ' -f1)
curl -s -X PUT --data-binary "$DATA" "$BASE/blocks/$H"; echo
say "4b. 地址与内容不符 → 400 hash_mismatch"
curl -s -X PUT --data-binary 'tampered' "$BASE/blocks/$H"; echo

say "5. 下载与存在性检查"
curl -s "$BASE/blocks/$P"; echo
curl -s -I "$BASE/blocks/$P" | head -4
curl -s "$BASE/blocks/$P?verify=1" >/dev/null && echo "verify=1 OK"

say "6. 发布根 main -> v1 树"
curl -s -X PUT -H "X-Block-Hash: $P" "$BASE/roots/main"; echo

say "7. 构造 v2 树并切换根 (原子发布)"
L3=$(curl -s -X POST --data-binary 'leaf-three' "$BASE/blocks" | python3 -c 'import sys,json;print(json.load(sys.stdin)["hash"])')
P2=$(curl -s -X POST -H "X-Refs: $L1,$L3" --data-binary 'tree-v2' "$BASE/blocks" | python3 -c 'import sys,json;print(json.load(sys.stdin)["hash"])')
curl -s -X PUT -H "X-Block-Hash: $P2" "$BASE/roots/main"; echo
curl -s "$BASE/roots"; echo

say "8. 制造孤儿块 (上传但不挂根)"
ORPHAN=$(curl -s -X POST --data-binary 'never-referenced' "$BASE/blocks" | python3 -c 'import sys,json;print(json.load(sys.stdin)["hash"])')
echo "orphan = $ORPHAN"

say "9. GC 预演 (dry_run=1): 只报告, 不删除"
curl -s -X POST "$BASE/gc?dry_run=1"; echo
curl -s -o /dev/null -w "orphan still present: %{http_code}\n" "$BASE/blocks/$ORPHAN"

say "10. GC 实扫 (dry_run=0): 孤儿与旧树独有块被回收, 可达块保留"
curl -s -X POST "$BASE/gc?dry_run=0"; echo
curl -s -o /dev/null -w "orphan after sweep: %{http_code} (expect 404)\n" "$BASE/blocks/$ORPHAN"
curl -s -o /dev/null -w "shared leaf L1:  %{http_code} (expect 200)\n" "$BASE/blocks/$L1"
curl -s -o /dev/null -w "v2-only leaf L3: %{http_code} (expect 200)\n" "$BASE/blocks/$L3"
curl -s -o /dev/null -w "v1-only leaf L2: %{http_code} (expect 404, 已不可达)\n" "$BASE/blocks/$L2"

say "11. 完整性扫描"
curl -s -X POST "$BASE/verify"; echo

say "12. 删除根后再次 GC → 全部回收"
curl -s -X DELETE "$BASE/roots/main"; echo
curl -s -X POST "$BASE/gc?dry_run=0"; echo
