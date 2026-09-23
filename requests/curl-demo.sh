#!/usr/bin/env bash
# imerkle 端到端演示：打开仓库 → 写文件 → 首/尾/全范围证明与验证
#                        → 增量更新 → 攻击场景（篡改/伪造长度/错位/伪造根）
# 用法：先启动服务（另一个终端）：
#   cargo run --release -- serve --addr 127.0.0.1:8099 --data-dir ./data
# 然后运行： bash requests/curl-demo.sh
set -euo pipefail

B="${IMERKLE_BASE:-http://127.0.0.1:8099}"
REPO="${IMERKLE_REPO:-demo}"
CS=16
py() { python3 - "$@"; }

echo "== 1) 打开/创建仓库（chunk_size=$CS） =="
curl -s -X POST "$B/repos/$REPO/open?chunk_size=$CS" | python3 -m json.tool

echo "== 2) 空文件状态（注意空文件也有确定的承诺根） =="
curl -s "$B/repos/$REPO" | python3 -m json.tool

# 50 字节 = 3 个满块(16) + 1 个 2 字节尾块
HEX=$(python3 -c "print(bytes(range(50)).hex())")
echo "== 3) 写入 50 字节（4 块），hex 负载 =="
curl -s -X PUT "$B/repos/$REPO/file" \
  -H 'Content-Type: application/json' \
  -d "{\"data\":\"$HEX\"}" | python3 -m json.tool

echo "== 4) 首块证明 [0,1)（chunks 仅 1 块，不含完整文件） =="
curl -s "$B/repos/$REPO/proof?start=0&end=1" > /tmp/imerkle_head.json
python3 -c "import json;p=json.load(open('/tmp/imerkle_head.json'));print('chunks:',len(p['chunks']),'steps:',len(p['steps']),'root:',p['root'][:16],'...')"

echo "== 5) 独立验证首块证明 =="
curl -s -X POST "$B/verify" -H 'Content-Type: application/json' \
  --data @/tmp/imerkle_head.json | python3 -m json.tool

echo "== 6) 尾块证明 [3,4) 并验证（奇数块树的边界） =="
curl -s "$B/repos/$REPO/proof?start=3&end=4" > /tmp/imerkle_tail.json
curl -s -X POST "$B/verify" -H 'Content-Type: application/json' \
  --data @/tmp/imerkle_tail.json | python3 -m json.tool

echo "== 7) 全范围证明 [0,4) 并验证 =="
curl -s "$B/repos/$REPO/proof" > /tmp/imerkle_all.json
curl -s -X POST "$B/verify" -H 'Content-Type: application/json' \
  --data @/tmp/imerkle_all.json | python3 -m json.tool

echo "== 8) 增量更新：只改最后一块的 1 字节，chunks_written 应为 1 =="
HEX2=$(python3 -c "d=bytearray(range(50));d[48]^=0xff;print(d.hex())")
curl -s -X PUT "$B/repos/$REPO/file" -H 'Content-Type: application/json' \
  -d "{\"data\":\"$HEX2\"}" | python3 -m json.tool

echo "== 9) 攻击：篡改块内 1 字节 → valid=false =="
python3 - <<'PY'
import json,subprocess
p=json.load(open('/tmp/imerkle_head.json'))
b=bytearray(bytes.fromhex(p['chunks'][0])); b[0]^=0xff; p['chunks'][0]=b.hex()
print(subprocess.run(['curl','-s','-X','POST','http://127.0.0.1:8099/verify',
  '-H','Content-Type: application/json','-d',json.dumps(p)],
  capture_output=True,text=True).stdout)
PY

echo "== 10) 攻击：伪造文件长度 50→99 → valid=false（LengthMismatch） =="
python3 - <<'PY'
import json,subprocess
p=json.load(open('/tmp/imerkle_head.json')); p['file_length']=99
print(subprocess.run(['curl','-s','-X','POST','http://127.0.0.1:8099/verify',
  '-H','Content-Type: application/json','-d',json.dumps(p)],
  capture_output=True,text=True).stdout)
PY

echo "== 11) 攻击：错位（首块证明声称是尾块 [3,4)）→ valid=false =="
python3 - <<'PY'
import json,subprocess
p=json.load(open('/tmp/imerkle_head.json')); p['start_chunk']=3; p['end_chunk']=4
print(subprocess.run(['curl','-s','-X','POST','http://127.0.0.1:8099/verify',
  '-H','Content-Type: application/json','-d',json.dumps(p)],
  capture_output=True,text=True).stdout)
PY

echo "== 12) 攻击：伪造承诺根为全 0 → valid=false（RootMismatch） =="
python3 - <<'PY'
import json,subprocess
p=json.load(open('/tmp/imerkle_head.json')); p['root']='00'*32
print(subprocess.run(['curl','-s','-X','POST','http://127.0.0.1:8099/verify',
  '-H','Content-Type: application/json','-d',json.dumps(p)],
  capture_output=True,text=True).stdout)
PY

echo "== 13) 空文件仓库的范围证明 → HTTP 400 =="
curl -s -X POST "$B/repos/blank/open?chunk_size=$CS" >/dev/null
curl -s -w "\n[HTTP %{http_code}]\n" "$B/repos/blank/proof"
